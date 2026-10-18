from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from wortling.api import create_app
from wortling.browser_auth import COOKIE, issue_setup_link
from wortling.db import Database, now

BASE = "https://wortling.test"
ORIGIN = {"Origin": BASE, "X-Wortling-Request": "1"}


def setup(settings):
    db = Database(settings.data_dir)
    db.initialize(settings)
    return db, issue_setup_link(db, "test", BASE).split("#")[1]


def test_setup_link_creates_persistent_cookie_and_signout_revokes(settings):
    db, token = setup(settings)
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.get("/setup").status_code == 200
        # GET previews never consume a link.
        assert client.get("/setup#" + token).status_code == 200
        result = client.post("/v1/session", json={"token": token}, headers=ORIGIN)
        assert result.json() == {"learner": "Test"}
        cookie = result.headers["set-cookie"]
        assert all(
            value in cookie
            for value in ["HttpOnly", "Secure", "SameSite=strict", "Max-Age=7776000"]
        )
        assert "Domain=" not in cookie
        assert client.get("/v1/report").json()["learner"] == "Test"
        assert client.post("/v1/session", json={"token": token}, headers=ORIGIN).status_code == 401
        saved_cookie = client.cookies.get(COOKIE)
        assert (
            client.post(
                "/v1/captures", headers=ORIGIN, json={"text": "Die Sitzung.", "client_ts": now()}
            ).status_code
            == 202
        )
        assert (
            client.post("/v1/captures", json={"text": "test", "client_ts": now()}).status_code
            == 403
        )
        assert client.delete("/v1/session", headers=ORIGIN).status_code == 200
        assert client.get("/v1/report").status_code == 401
        assert (
            client.get("/v1/report", headers={"Cookie": f"{COOKIE}={saved_cookie}"}).status_code
            == 401
        )
    with db.connect() as conn:
        assert token not in str([tuple(row) for row in conn.execute("SELECT * FROM setup_links")])
        assert conn.execute("SELECT COUNT(*) FROM browser_sessions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "headers", [{}, {"Origin": "https://evil.test", "X-Wortling-Request": "1"}, {"Origin": BASE}]
)
def test_redeem_requires_same_origin_custom_request(settings, headers):
    _, token = setup(settings)
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.post("/v1/session", json={"token": token}, headers=headers).status_code == 403
        assert client.post("/v1/session", json={"token": token}, headers=ORIGIN).status_code == 200


def test_session_expiry_and_token_rotation(settings, monkeypatch):
    db, token = setup(settings)
    with TestClient(create_app(settings), base_url=BASE) as client:
        client.post("/v1/session", json={"token": token}, headers=ORIGIN).raise_for_status()
        with db.connect() as conn:
            conn.execute("UPDATE browser_sessions SET expires_at='2000-01-01'")
        assert client.get("/v1/report").status_code == 401
        token = issue_setup_link(db, "test", BASE).split("#")[1]
        client.post("/v1/session", json={"token": token}, headers=ORIGIN).raise_for_status()
        unused = issue_setup_link(db, "test", BASE).split("#")[1]
        monkeypatch.setenv("TEST_CAPTURE_TOKEN", "b" * 48)
        db.initialize(settings)
        assert client.get("/v1/report").status_code == 401
        assert client.post("/v1/session", json={"token": unused}, headers=ORIGIN).status_code == 401
        assert (
            client.get("/v1/report", headers={"Authorization": "Bearer " + "b" * 48}).status_code
            == 200
        )


def test_expired_setup_link_and_concurrent_redemption(settings):
    db, token = setup(settings)
    with db.connect() as conn:
        conn.execute("UPDATE setup_links SET expires_at='2000-01-01'")
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.post("/v1/session", json={"token": token}, headers=ORIGIN).status_code == 401
    token = issue_setup_link(db, "test", BASE).split("#")[1]

    def redeem(_):
        with TestClient(create_app(settings), base_url=BASE) as client:
            return client.post("/v1/session", json={"token": token}, headers=ORIGIN).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(redeem, range(2))) == [200, 401]


def test_sessions_are_scoped_to_their_learner(settings, monkeypatch):
    from wortling.config import Learner

    monkeypatch.setenv("SECOND_CAPTURE_TOKEN", "c" * 48)
    settings.users.append(
        Learner(
            id="other",
            display_name="Other",
            capture_token_env="SECOND_CAPTURE_TOKEN",
            ankiweb_user_env="OTHER_ANKI_USER",
            ankiweb_pass_env="OTHER_ANKI_PASS",
        )
    )
    db, token = setup(settings)
    with TestClient(create_app(settings), base_url=BASE) as first:
        first.post("/v1/session", json={"token": token}, headers=ORIGIN).raise_for_status()
        capture = first.post(
            "/v1/captures", headers=ORIGIN, json={"text": "private", "client_ts": now()}
        ).json()["id"]
    token = issue_setup_link(db, "other", BASE).split("#")[1]
    with TestClient(create_app(settings), base_url=BASE) as second:
        second.post("/v1/session", json={"token": token}, headers=ORIGIN).raise_for_status()
        assert second.get("/v1/report").json()["learner"] == "Other"
        assert second.get("/v1/report").json()["captures"] == []
        assert second.get("/v1/captures/" + capture).status_code == 404


def test_browser_setup_survives_new_tab_and_signout(settings):
    from playwright.sync_api import sync_playwright

    with TestClient(create_app(settings), base_url=BASE) as api, sync_playwright() as playwright:
        link = issue_setup_link(Database(settings.data_dir), "test", BASE)
        token = link.split("#")[1]
        browser = playwright.firefox.launch()
        context = browser.new_context()
        urls = []

        def dispatch(route):
            request = route.request
            urls.append(request.url)
            api.cookies.clear()
            response = api.request(
                request.method,
                request.url,
                headers=request.headers,
                content=request.post_data_buffer,
            )
            route.fulfill(
                status=response.status_code, headers=dict(response.headers), body=response.content
            )

        context.route(BASE + "/**", dispatch)
        page = context.new_page()
        page.goto(link)
        assert page.url == BASE + "/setup"
        page.get_by_role("button", name="Continue", exact=True).click()
        # Wait on the report section (#report is filled by the page's own script),
        # not on its heading copy: a layout change rewrote that copy on 2026-10-18
        # and turned this auth test red.
        page.locator("#report h2").wait_for()
        assert COOKIE not in page.evaluate("document.cookie")
        cookie = next(cookie for cookie in context.cookies() if cookie["name"] == COOKIE)
        assert cookie["httpOnly"] and cookie["secure"]
        assert cookie["expires"] > 0
        second = context.new_page()
        second.goto(BASE)
        second.locator("#report h2").wait_for()
        second.get_by_role("button", name="Sign out", exact=True).click()
        second.wait_for_function("document.querySelector('#status').textContent==='Signed out.'")
        second.reload()
        second.locator("#signin").wait_for(state="visible")
        assert not context.cookies()
        assert all(token not in url for url in urls)
        browser.close()
