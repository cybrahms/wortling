import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from wortling.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CAPTURE_TOKEN", "a" * 48)
    return Settings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "users": [
                {
                    "id": "test",
                    "display_name": "Test",
                    "capture_token_env": "TEST_CAPTURE_TOKEN",
                    "ankiweb_user_env": "TEST_ANKI_USER",
                    "ankiweb_pass_env": "TEST_ANKI_PASS",
                }
            ],
        }
    )


@pytest.fixture
def sync_endpoint(tmp_path):
    if endpoint := os.environ.get("WORTLING_TEST_SYNC_ENDPOINT"):
        assert endpoint.startswith("http://127.0.0.1:")
        yield endpoint
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "SYNC_USER1": "test:test",
        "SYNC_BASE": str(tmp_path / "server"),
        "SYNC_HOST": "127.0.0.1",
        "SYNC_PORT": str(port),
        "RUST_LOG": "error",
    }
    endpoint = f"http://127.0.0.1:{port}/"
    with (tmp_path / "sync.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "anki.syncserver"], env=env, stdout=log, stderr=log
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, (tmp_path / "sync.log").read_text()
                try:
                    httpx.get(endpoint, timeout=0.2)
                    break
                except httpx.TransportError:
                    if time.monotonic() > deadline:
                        raise AssertionError("Test sync server did not start") from None
                    time.sleep(0.05)
            yield endpoint
        finally:
            process.terminate()
            process.wait(timeout=5)
