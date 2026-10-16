from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from wortling.anki.reader import open_collection, rebuild_index
from wortling.anki.sync import incremental_sync, login
from wortling.anki.writer import write_session
from wortling.backup import backup_all
from wortling.config import Settings
from wortling.db import Database, now
from wortling.report import write_report


def tick(settings: Settings, db: Database) -> None:
    with db.connect() as conn:
        last_backup = conn.execute(
            "SELECT created_at,status FROM backup_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    backup_due = not last_backup or last_backup[0][:10] != now()[:10]
    if last_backup and last_backup[1] != "complete":
        backup_due = datetime.fromisoformat(last_backup[0]) < datetime.now(UTC) - timedelta(hours=1)
    if backup_due:
        try:
            with FileLock(settings.data_dir / "nightly-backup.lock", timeout=0):
                backup_all(settings, db)
        except Timeout:
            pass
        except Exception:
            logging.exception("Nightly backup failed; alert recorded, retry in one hour")
    for user in settings.users:
        timestamp = now()
        day = datetime.now(ZoneInfo(user.timezone)).date().isoformat()
        with db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO scheduler_state(user_id,next_sync_at) VALUES (?,?)",
                (user.id, timestamp),
            )
            state = dict(
                conn.execute("SELECT * FROM scheduler_state WHERE user_id=?", (user.id,)).fetchone()
            )
        if state["last_report_day"] != day:
            write_report(db, user)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE scheduler_state SET last_report_day=? WHERE user_id=?", (day, user.id)
                )
        exists = (settings.data_dir / "collections" / user.id / "collection.anki2").exists()
        if state["next_sync_at"] > timestamp or not exists:
            continue
        try:
            if user.writes_enabled:
                write_session(settings, db, user)
            else:
                with open_collection(settings.data_dir, user.id) as collection:
                    auth = login(collection, user, settings.anki_sync_endpoint)
                    incremental_sync(collection, auth)
                    rebuild_index(db, user, collection)
        except Exception as exc:
            logging.error("Scheduled Anki session failed for %s: %s", user.id, exc)
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO alerts(user_id,kind,message,created_at) VALUES (?,?,?,?)",
                    (user.id, "sync_failed", str(exc)[:1000], now()),
                )
                next_attempt = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
                conn.execute(
                    "UPDATE scheduler_state SET next_sync_at=? WHERE user_id=?",
                    (next_attempt, user.id),
                )
        else:
            next_attempt = (
                datetime.now(UTC) + timedelta(hours=settings.write_interval_hours)
            ).isoformat()
            with db.connect() as conn:
                conn.execute(
                    "UPDATE scheduler_state SET next_sync_at=?,last_sync_at=? WHERE user_id=?",
                    (next_attempt, now(), user.id),
                )
