import sqlite3
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from wortling.api import create_app
from wortling.config import Settings
from wortling.db import Database


def test_health_and_repeat_start(settings):
    for _ in range(2):
        with TestClient(create_app(settings)) as client:
            assert client.get("/v1/health").json()["status"] == "ok"
    with Database(settings.data_dir).connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        expected = {
            path.name
            for path in files("wortling").joinpath("migrations").iterdir()
            if path.name.endswith(".sql")
        }
        assert {row[0] for row in conn.execute("SELECT version FROM schema_migrations")} == expected
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_concurrent_initialization(settings):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: Database(settings.data_dir).initialize(settings), range(2)))


def test_duplicate_users_rejected(settings):
    data = settings.model_dump()
    data["users"] *= 2
    with pytest.raises(ValidationError, match="duplicate"):
        Settings.model_validate(data)


def test_secret_length_and_collision(settings, monkeypatch):
    monkeypatch.setenv("TEST_CAPTURE_TOKEN", "short")
    with pytest.raises(ValueError, match="32 characters"):
        Database(settings.data_dir).initialize(settings)


def test_backup_is_consistent(settings, tmp_path):
    db = Database(settings.data_dir)
    db.initialize(settings)
    target = tmp_path / "backup.sqlite3"
    db.backup(target)
    with sqlite3.connect(target) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT id FROM users").fetchone()[0] == "test"


def test_initialization_lock_covers_wal_setup(settings):
    from concurrent.futures import TimeoutError

    from filelock import FileLock

    settings.data_dir.mkdir()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with FileLock(settings.data_dir / "initialize.lock"):
            future = pool.submit(Database(settings.data_dir).initialize, settings)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.15)
            assert not (settings.data_dir / "wortling.sqlite3").exists()
        future.result(timeout=5)
