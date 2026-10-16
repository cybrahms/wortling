import socket
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from httpcore._backends.sync import SyncBackend

from wortling.api import create_app
from wortling.db import Database, now
from wortling.pipeline.engine import Pipeline
from wortling.sources import (
    MAX_SOURCE_BYTES,
    FetchedSource,
    PublicNetworkBackend,
    UnsafeSource,
    checked_url,
    fetch_source,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://100.107.210.54/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "https://example.com:8443/",
        "ftp://example.com/a",
        "https://user:pass@example.com/",
    ],
)
def test_non_public_or_credentialed_sources_rejected(url):
    with pytest.raises(UnsafeSource):
        checked_url(url)


def test_dns_is_checked_and_connection_pinned(monkeypatch):
    calls = []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(SyncBackend, "connect_tcp", lambda self, host, *args: calls.append(host))
    PublicNetworkBackend().connect_tcp("example.com", 443)
    assert calls == ["93.184.216.34"]
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
    )
    with pytest.raises(UnsafeSource):
        PublicNetworkBackend().connect_tcp("example.com", 443)
    assert calls == ["93.184.216.34"]


def test_redirect_to_private_address_is_blocked():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    )
    with pytest.raises(UnsafeSource):
        fetch_source("https://example.com/", transport=transport)


def test_source_limits_and_html_extraction():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                "<nav>Menu</nav><script>ignore()</script><p>Die Behörde ist geöffnet.</p>"
            ).encode(),
        )
    )
    result = fetch_source("https://example.com/", transport=transport)
    assert result.text == "Die Behörde ist geöffnet."
    oversized = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"x" * (MAX_SOURCE_BYTES + 1)
        )
    )
    with pytest.raises(ValueError, match="document limit"):
        fetch_source("https://example.com/", transport=oversized)


def test_url_capture_uses_archived_source_on_reprocess(settings, monkeypatch):
    from test_pipeline import RecordedClient

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/captures",
            headers={"Authorization": "Bearer " + "a" * 48},
            json={"url": "https://example.com/", "client_ts": now()},
        )
        assert response.status_code == 202
    calls = []

    def fetch(url):
        calls.append(url)
        return FetchedSource(
            url, "text/plain", b"Die Sitzung beginnt morgen.", "Die Sitzung beginnt morgen."
        )

    monkeypatch.setattr("wortling.pipeline.engine.fetch_source", fetch)
    db = Database(settings.data_dir)
    pipeline = Pipeline(settings, db, RecordedClient())
    pipeline.process(response.json()["id"])
    pipeline.process(response.json()["id"])
    assert len(calls) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT text FROM captures").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM capture_sources").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE capture_sources SET source_text='changed'")
