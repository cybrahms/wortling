import base64
import io
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wortling.api import create_app
from wortling.db import Database

AUTH = {"Authorization": "Bearer " + "a" * 48}


def payload(**kwargs):
    return {
        "text": "Die Sitzung beginnt morgen.",
        "client_ts": datetime.now(UTC).isoformat(),
        **kwargs,
    }


def test_capture_is_durable_idempotent_and_immutable(settings):
    data = payload()
    with TestClient(create_app(settings)) as client:
        headers = {**AUTH, "Idempotency-Key": "shortcut-attempt-1"}
        response = client.post("/v1/captures", json=data, headers=headers)
        assert response.status_code == 202
        assert client.post("/v1/captures", json=data, headers=headers).json() == response.json()
        assert (
            client.post("/v1/captures", json=payload(text="Anders"), headers=headers).status_code
            == 409
        )
    with Database(settings.data_dir).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "pending"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE captures SET text='changed'")


def test_auth_and_user_isolation(settings, monkeypatch):
    from wortling.config import Learner

    monkeypatch.setenv("OTHER_TOKEN", "b" * 48)
    settings.users.append(
        Learner(
            id="other",
            display_name="Other",
            capture_token_env="OTHER_TOKEN",
            ankiweb_user_env="OTHER_USER",
            ankiweb_pass_env="OTHER_PASS",
        )
    )
    with TestClient(create_app(settings)) as client:
        assert client.post("/v1/captures", json=payload()).status_code == 401
        assert (
            client.post(
                "/v1/captures", json=payload(), headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        capture_id = client.post("/v1/captures", json=payload(), headers=AUTH).json()["id"]
        assert client.get(f"/v1/captures/{capture_id}", headers=AUTH).status_code == 200
        assert (
            client.get(
                f"/v1/captures/{capture_id}", headers={"Authorization": "Bearer " + "b" * 48}
            ).status_code
            == 404
        )


def test_image_stored_and_validated(settings):
    buffer = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(buffer, format="JPEG")
    image = base64.b64encode(buffer.getvalue()).decode()
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/v1/captures",
                json=payload(text=None, image_b64=image, image_mime="image/jpeg"),
                headers=AUTH,
            ).status_code
            == 202
        )
        assert (
            client.post(
                "/v1/captures", json=payload(image_b64=image, image_mime="image/png"), headers=AUTH
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/captures",
                json=payload(image_b64="nonsense", image_mime="image/jpeg"),
                headers=AUTH,
            ).status_code
            == 422
        )
    assert len(list((settings.data_dir / "images" / "test").glob("*.jpg"))) == 1


def test_body_limit_and_rate_limit(settings):
    settings.max_body_bytes = 1024
    settings.capture_limit_per_minute = 1
    with TestClient(create_app(settings)) as client:
        assert client.post("/v1/captures", content="x" * 1025, headers=AUTH).status_code == 413
        assert client.post("/v1/captures", json=payload(), headers=AUTH).status_code == 202
        response = client.post("/v1/captures", json=payload(), headers=AUTH)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"


@pytest.mark.parametrize(
    "invalid",
    [
        {"text": None},
        {"image_b64": "x"},
        {"image_mime": "image/jpeg"},
        {"url": "file:///etc/passwd"},
        {"client_ts": "2026-10-14T10:00:00"},
    ],
)
def test_payload_validation(settings, invalid):
    with TestClient(create_app(settings)) as client:
        assert client.post("/v1/captures", json=payload(**invalid), headers=AUTH).status_code == 422
