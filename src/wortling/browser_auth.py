from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from wortling.config import StrictModel
from wortling.db import Database, now, token_hash

COOKIE = "__Host-wortling"
SESSION_SECONDS = 90 * 24 * 60 * 60
router = APIRouter(prefix="/v1/session")


def require_browser_origin(request: Request) -> None:
    origin = urlsplit(request.headers.get("origin", ""))
    if (
        origin.scheme != "https"
        or origin.netloc != request.headers.get("host")
        or origin.path not in {"", "/"}
        or request.headers.get("x-wortling-request") != "1"
    ):
        raise HTTPException(403, "Open Wortling directly to perform this action")


def issue_setup_link(db: Database, user_id: str, base_url: str) -> str:
    url = urlsplit(base_url)
    if url.scheme != "https" or not url.netloc or url.username or url.password:
        raise ValueError("Setup links require an HTTPS base URL without credentials")
    if url.path not in {"", "/"} or url.query or url.fragment:
        raise ValueError("Base URL must not include a path, query or fragment")
    token = secrets.token_urlsafe(32)
    timestamp = now()
    expires = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
    with db.connect() as conn:
        user = conn.execute("SELECT token_hash FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise ValueError("Unknown learner")
        conn.execute(
            "INSERT INTO setup_links VALUES (?,?,?,?,?,NULL)",
            (token_hash(token), user_id, user[0], timestamp, expires),
        )
    return base_url.rstrip("/") + "/setup#" + token


class Redeem(StrictModel):
    token: str = Field(min_length=32, max_length=128)


@router.post("")
def redeem(payload: Redeem, request: Request, response: Response):
    require_browser_origin(request)
    db = request.app.state.db
    timestamp = now()
    session = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(seconds=SESSION_SECONDS)).isoformat()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        link = conn.execute(
            "SELECT l.* FROM setup_links l JOIN users u ON u.id=l.user_id "
            "WHERE l.token_hash=? AND l.used_at IS NULL AND l.expires_at>? "
            "AND l.credential_hash IS u.token_hash",
            (token_hash(payload.token), timestamp),
        ).fetchone()
        if not link:
            raise HTTPException(
                401, "This setup link has expired or was already used. Ask for a new one."
            )
        user = next((u for u in request.app.state.settings.users if u.id == link["user_id"]), None)
        if user is None:
            raise HTTPException(401, "This learner is no longer configured")
        conn.execute(
            "UPDATE setup_links SET used_at=? WHERE token_hash=?", (timestamp, link["token_hash"])
        )
        conn.execute(
            "INSERT INTO browser_sessions VALUES (?,?,?,?,?)",
            (token_hash(session), user.id, link["credential_hash"], timestamp, expires),
        )
        if previous := request.cookies.get(COOKIE):
            conn.execute("DELETE FROM browser_sessions WHERE token_hash=?", (token_hash(previous),))
    response.set_cookie(
        COOKIE,
        session,
        max_age=SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"learner": user.display_name}


@router.delete("")
def sign_out(request: Request, response: Response):
    require_browser_origin(request)
    if token := request.cookies.get(COOKIE):
        with request.app.state.db.connect() as conn:
            conn.execute("DELETE FROM browser_sessions WHERE token_hash=?", (token_hash(token),))
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"signed_out": True}


def session_user_id(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 128:
        return None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_browser_origin(request)
    with request.app.state.db.connect() as conn:
        row = conn.execute(
            "SELECT s.user_id FROM browser_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND s.credential_hash IS u.token_hash",
            (token_hash(token), now()),
        ).fetchone()
    return row[0] if row else None
