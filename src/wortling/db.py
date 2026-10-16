from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from filelock import FileLock

from wortling.config import Settings


def now() -> str:
    return datetime.now(UTC).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "wortling.sqlite3"

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self, settings: Settings) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        with FileLock(self.data_dir / "initialize.lock", timeout=30):
            self._initialize(settings)

    def _initialize(self, settings: Settings) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # Exclusive migration lock, shared by API and worker startup.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
            for migration in sorted(
                files("wortling").joinpath("migrations").iterdir(), key=lambda entry: entry.name
            ):
                if not migration.name.endswith(".sql"):
                    continue
                if conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (migration.name,)
                ).fetchone():
                    continue
                # executescript commits first: execute complete statements individually.
                statement = ""
                for line in migration.read_text().splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        conn.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError(f"incomplete migration: {migration.name}")
                conn.execute("INSERT INTO schema_migrations VALUES (?)", (migration.name,))
            for user in settings.users:
                token = os.environ.get(user.capture_token_env)
                if token is not None and len(token) < 32:
                    raise ValueError(
                        f"{user.capture_token_env} must contain at least 32 characters"
                    )
                conn.execute(
                    "INSERT INTO users(id,name,token_hash) VALUES (?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name,token_hash=excluded.token_hash",
                    (user.id, user.display_name, token_hash(token) if token else None),
                )
        self.path.chmod(0o600)

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        destination.chmod(0o600)
