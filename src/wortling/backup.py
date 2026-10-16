from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import ExitStack, closing
from pathlib import Path

import yaml
from filelock import FileLock

from wortling.config import Settings
from wortling.db import Database, now


def backup_all(settings: Settings, db: Database) -> Path:
    backup_id = str(uuid.uuid4())
    base = settings.data_dir / "backups" / "nightly"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = base / backup_id
    target.mkdir(mode=0o700)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO backup_runs VALUES (?,?,?,'running',NULL)", (backup_id, now(), str(target))
        )
    try:
        with ExitStack() as locks:
            for user in sorted(settings.users, key=lambda user: user.id):
                folder = settings.data_dir / "collections" / user.id
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                locks.enter_context(FileLock(folder / "collection.lock", timeout=30))
            db.backup(target / "wortling.sqlite3")
            with closing(sqlite3.connect(target / "wortling.sqlite3")) as snapshot:
                assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                images = snapshot.execute(
                    "SELECT image_path FROM captures WHERE image_path IS NOT NULL"
                )
                for (image,) in images:
                    destination = target / image
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(settings.data_dir / image, destination)
            for user in settings.users:
                folder = settings.data_dir / "collections" / user.id
                if not (folder / "collection.anki2").exists():
                    continue
                destination = target / "collections" / user.id
                destination.mkdir(parents=True, mode=0o700)
                with closing(sqlite3.connect(folder / "collection.anki2")) as source:
                    with closing(sqlite3.connect(destination / "collection.anki2")) as snapshot:
                        source.backup(snapshot)
                media_db = folder / "collection.media.db2"
                if media_db.exists():
                    with closing(sqlite3.connect(media_db)) as source:
                        with closing(sqlite3.connect(destination / media_db.name)) as snapshot:
                            source.backup(snapshot)
                media = folder / "collection.media"
                if media.exists():
                    shutil.copytree(media, destination / "collection.media")
        (target / "config.snapshot.yaml").write_text(
            yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        )
        manifest = {}
        for path in target.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
                manifest[str(path.relative_to(target))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        manifest_path = target / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest_path.chmod(0o600)
        # Optional off-host encryption/storage. Credentials remain environment-only.
        if os.environ.get("RESTIC_REPOSITORY"):
            subprocess.run(
                ["restic", "backup", str(target), "--tag", "wortling"],
                check=True,
                timeout=1800,
                capture_output=True,
            )
        with db.connect() as conn:
            conn.execute("UPDATE backup_runs SET status='complete' WHERE id=?", (backup_id,))
        for old in sorted(
            (path for path in base.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
        )[:-7]:
            shutil.rmtree(old)
        return target
    except Exception as exc:
        with db.connect() as conn:
            conn.execute(
                "UPDATE backup_runs SET status='error',error=? WHERE id=?",
                (str(exc)[:1000], backup_id),
            )
            conn.execute(
                "INSERT INTO alerts(user_id,kind,message,created_at) VALUES (NULL,?,?,?)",
                ("backup_failed", str(exc)[:1000], now()),
            )
        raise


def verify_backup(path: Path) -> int:
    manifest = json.loads((path / "manifest.json").read_text())
    for name, digest in manifest.items():
        candidate = (path / name).resolve()
        if not candidate.is_relative_to(path.resolve()):
            raise ValueError("Backup manifest path escapes its directory")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Backup checksum mismatch: {name}")
    with closing(sqlite3.connect(path / "wortling.sqlite3")) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup database integrity check failed")
    return len(manifest)
