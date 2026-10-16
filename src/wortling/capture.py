from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import uuid
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from wortling.config import Learner, StrictModel
from wortling.db import now, token_hash

router = APIRouter(prefix="/v1")


class Capture(StrictModel):
    text: str | None = Field(default=None, max_length=100_000)
    url: HttpUrl | None = None
    image_b64: str | None = None
    image_mime: str | None = None
    client_ts: AwareDatetime

    @model_validator(mode="after")
    def content_present(self) -> Capture:
        if not (self.text and self.text.strip()) and not self.url and not self.image_b64:
            raise ValueError("capture requires text, a URL, or an image")
        if bool(self.image_b64) != bool(self.image_mime):
            raise ValueError("image_b64 and image_mime must be supplied together")
        if self.image_mime and self.image_mime not in {"image/jpeg", "image/png"}:
            raise ValueError("supported image types: image/jpeg, image/png")
        return self


def learner_auth(request: Request, authorization: str | None = Header(default=None)) -> Learner:
    if authorization is None:
        from wortling.browser_auth import session_user_id

        user_id = session_user_id(request)
        for learner in request.app.state.settings.users:
            if learner.id == user_id:
                return learner
        raise HTTPException(401, "Open your personal setup link to sign in")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        raise HTTPException(401, "Invalid capture token", headers={"WWW-Authenticate": "Bearer"})
    with request.app.state.db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE token_hash=?", (token_hash(token),)
        ).fetchone()
    if row:
        for learner in request.app.state.settings.users:
            if learner.id == row["id"]:
                return learner
    raise HTTPException(401, "Invalid capture token", headers={"WWW-Authenticate": "Bearer"})


def validate_image(payload: Capture) -> bytes | None:
    if payload.image_b64 is None:
        return None
    try:
        data = base64.b64decode(payload.image_b64, validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                expected = "JPEG" if payload.image_mime == "image/jpeg" else "PNG"
                if image.format != expected or image.width * image.height > 20_000_000:
                    raise ValueError("image format mismatch or more than 20 million pixels")
                image.verify()
    except (
        ValueError,
        binascii.Error,
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise HTTPException(422, "Invalid image or image exceeds pixel limit") from exc
    return data


@router.post("/captures", status_code=202)
def capture(
    payload: Capture,
    request: Request,
    learner: Annotated[Learner, Depends(learner_auth)],
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    db = request.app.state.db
    fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    image_data = validate_image(payload)
    capture_id = str(uuid.uuid4())
    image_path: Path | None = None
    try:
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                old = conn.execute(
                    "SELECT id,payload_hash FROM captures WHERE user_id=? AND idempotency_key=?",
                    (learner.id, idempotency_key),
                ).fetchone()
                if old:
                    if old["payload_hash"] != fingerprint:
                        raise HTTPException(409, "Idempotency key reused with different content")
                    return {"id": old["id"], "status": "captured"}
            cutoff = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
            count = conn.execute(
                "SELECT COUNT(*) FROM captures WHERE user_id=? AND received_at>?",
                (learner.id, cutoff),
            ).fetchone()[0]
            if count >= request.app.state.settings.capture_limit_per_minute:
                raise HTTPException(
                    429, "Capture rate limit reached", headers={"Retry-After": "60"}
                )
            if image_data:
                folder = db.data_dir / "images" / learner.id
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                extension = ".jpg" if payload.image_mime == "image/jpeg" else ".png"
                image_path = folder / (capture_id + extension)
                with image_path.open("xb") as image_file:
                    image_file.write(image_data)
                    image_file.flush()
                    os.fsync(image_file.fileno())
                image_path.chmod(0o600)
            timestamp = now()
            conn.execute(
                "INSERT INTO captures(id,user_id,received_at,text,url,image_path,image_mime,"
                "client_ts,payload_hash,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    capture_id,
                    learner.id,
                    timestamp,
                    payload.text,
                    str(payload.url) if payload.url else None,
                    str(image_path.relative_to(db.data_dir)) if image_path else None,
                    payload.image_mime,
                    payload.client_ts.isoformat(),
                    fingerprint,
                    idempotency_key,
                ),
            )
            conn.execute(
                "INSERT INTO jobs(capture_id,next_attempt_at) VALUES (?,?)", (capture_id, timestamp)
            )
    except Exception:
        if image_path:
            image_path.unlink(missing_ok=True)
        raise
    return {"id": capture_id, "status": "captured"}


@router.get("/captures/{capture_id}")
def capture_status(
    capture_id: str, request: Request, learner: Annotated[Learner, Depends(learner_auth)]
):
    with request.app.state.db.connect() as conn:
        row = conn.execute(
            "SELECT id,received_at,text,url,status,error FROM captures WHERE id=? AND user_id=?",
            (capture_id, learner.id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Capture not found")
    return dict(row)


class BodyLimit:
    """Bound actual request bytes, including chunked requests without Content-Length."""

    def __init__(self, app, limit: int):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    return await self.reject(send, 400, "Invalid Content-Length")
                if length > self.limit:
                    return await self.reject(send, 413, "Capture body exceeds 10 MB limit")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                return await self.reject(send, 413, "Capture body exceeds 10 MB limit")
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)

    @staticmethod
    async def reject(send, status: int, detail: str):
        body = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
