from datetime import UTC, datetime, timedelta

from test_pipeline import RecordedClient, capture

from wortling.db import Database, now
from wortling.pipeline.engine import Pipeline
from wortling.report import learner_report


def test_failed_capture_alert_is_scoped_and_clears_after_recovery(settings, monkeypatch):
    settings.users[0].processing_enabled = True
    failed = capture(settings, "Die Sitzung beginnt morgen.")
    other = capture(settings, "Eine andere Sitzung.")
    db = Database(settings.data_dir)
    pipeline = Pipeline(settings, db, RecordedClient())

    def fail(_):
        raise ValueError("Source mismatch")

    original = pipeline.process
    monkeypatch.setattr(pipeline, "process", fail)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET attempts=2 WHERE capture_id=?", (failed,))
    assert pipeline.run_once()
    report = learner_report(db, settings.users[0])
    assert len(report["alerts"]) == 1
    alert = report["alerts"][0]
    assert alert["capture_id"] == failed
    assert alert["capture_text"] == "Die Sitzung beginnt morgen."
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO alerts(user_id,kind,message,created_at,capture_id) VALUES (?,?,?,?,?)",
            ("test", "capture_failed", "Another failure", now(), other),
        )
        conn.execute(
            "INSERT INTO alerts(user_id,kind,message,created_at) VALUES (?,?,?,?)",
            ("test", "sync_failed", "Sync needs attention", now()),
        )
    original(failed)
    report = learner_report(db, settings.users[0])
    assert {a["message"] for a in report["alerts"]} == {"Another failure", "Sync needs attention"}
    with db.connect() as conn:
        assert conn.execute(
            "SELECT acknowledged_at FROM alerts WHERE id=?", (alert["id"],)
        ).fetchone()[0]


def test_migration_links_only_unambiguous_legacy_failures(settings):
    first = capture(settings, "Die Sitzung beginnt morgen.")
    other = capture(settings, "Noch eine Sitzung.")
    db = Database(settings.data_dir)
    Pipeline(settings, db, RecordedClient()).process(first)
    with db.connect() as conn:
        # Recreate the pre-migration alerts table to exercise upgrade with real history.
        conn.execute("DROP TABLE alerts")
        conn.execute(
            "CREATE TABLE alerts(id INTEGER PRIMARY KEY,user_id TEXT,kind TEXT,message TEXT,"
            "created_at TEXT,acknowledged_at TEXT)"
        )
        conn.execute("DELETE FROM schema_migrations WHERE version='005_capture_alerts.sql'")
        for run_id, cid, error in [
            ("one", first, "Unique"),
            ("two", first, "Ambiguous"),
            ("three", other, "Ambiguous"),
        ]:
            conn.execute(
                "INSERT INTO processing_runs(id,capture_id,prompt_version,status,started_at,"
                "finished_at,error) VALUES (?,?,?,'error',?,?,?)",
                (run_id, cid, "{}", now(), now(), error),
            )
        for message in ["Unique", "Ambiguous", "Unrelated"]:
            conn.execute(
                "INSERT INTO alerts(user_id,kind,message,created_at) VALUES (?,?,?,?)",
                ("test", "capture_failed", message, now()),
            )
    db.initialize(settings)
    report = learner_report(db, settings.users[0])
    assert {a["message"] for a in report["alerts"]} == {"Ambiguous", "Unrelated"}
    assert all(a["capture_id"] is None for a in report["alerts"])
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE message='Unique'").fetchone()
        assert row["capture_id"] == first
        assert row["acknowledged_at"]


def test_a_recovered_sync_clears_its_own_alert(settings, monkeypatch, sync_endpoint):
    """The failure that paged the operator for two days: one rejected request
    left a `sync_failed` alert open, and the operational check counts an open
    alert as a standing issue. A sync that works again clears its own alert,
    the way a processed capture clears `capture_failed`."""
    from test_anki_sync import ready

    from wortling import scheduler

    def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler, "backup_all", quiet)
    monkeypatch.setattr(scheduler, "write_report", quiet)
    monkeypatch.setattr(scheduler, "login", lambda collection, user, endpoint: object())
    db, user = ready(settings, monkeypatch, sync_endpoint)
    user.writes_enabled = False

    def refused(collection, auth):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(scheduler, "incremental_sync", refused)
    scheduler.tick(settings, db)
    with db.connect() as conn:
        open_alerts = conn.execute(
            "SELECT message FROM alerts WHERE kind='sync_failed' AND acknowledged_at IS NULL"
        ).fetchall()
    assert len(open_alerts) == 1
    # Which failure this was, in the alert a person reads: not AnkiWeb's words
    # about a clock, but the network underneath them going away.
    assert open_alerts[0]["message"].startswith("could not reach AnkiWeb")

    # The retry the schedule put fifteen minutes out, brought forward.
    with db.connect() as conn:
        conn.execute(
            "UPDATE scheduler_state SET next_sync_at=? WHERE user_id=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), user.id),
        )
    monkeypatch.setattr(scheduler, "incremental_sync", lambda collection, auth: None)
    monkeypatch.setattr(scheduler, "rebuild_index", lambda *args: None)
    scheduler.tick(settings, db)

    with db.connect() as conn:
        assert conn.execute(
            "SELECT acknowledged_at FROM alerts WHERE kind='sync_failed'"
        ).fetchone()[0], "a working sync clears the alert of its own kind"
        assert conn.execute("SELECT last_sync_at FROM scheduler_state").fetchone()[0]


def test_a_completed_write_clears_the_write_alert(settings, monkeypatch, sync_endpoint):
    from test_anki_sync import pending, ready

    from wortling.anki.writer import write_session

    db, user = ready(settings, monkeypatch, sync_endpoint)
    pending(db, user)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO alerts(user_id,kind,message,created_at) VALUES (?,?,?,?)",
            (user.id, "write_failed", "could not reach AnkiWeb: reset by peer", now()),
        )
    assert write_session(settings, db, user)["status"] == "complete"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT acknowledged_at FROM alerts WHERE kind='write_failed'"
        ).fetchone()[0]


def test_the_alert_says_whether_ankiweb_refused_or_was_unreachable():
    """AnkiWeb's own sentence about a clock reached the operator as the whole
    story, so a server that had answered read exactly like a host that could not
    reach it. A refusal and an unreachable server are different repairs."""
    from anki.errors import NetworkError, SyncError, SyncErrorKind

    from wortling.anki.sync import FullSyncRequired, describe_failure

    refused = SyncError(
        "Unable to sync - your clock is not set to the correct time.",
        "",
        "",
        "",
        SyncErrorKind.OTHER,
    )
    assert describe_failure(refused) == (
        "AnkiWeb refused this request: Unable to sync - your clock is not set to the correct time."
    )
    assert describe_failure(NetworkError("dns failure", "", "", "")).startswith(
        "could not reach AnkiWeb: dns failure"
    )
    assert describe_failure(ConnectionResetError("reset by peer")).startswith(
        "could not reach AnkiWeb:"
    )
    assert describe_failure(TimeoutError("handshake timed out")).startswith(
        "could not reach AnkiWeb:"
    )
    assert describe_failure(FullSyncRequired("Full sync required")).startswith(
        "AnkiWeb asked for a full sync"
    )
    # A local failure is not blamed on the network it never touched.
    assert describe_failure(RuntimeError("Missing Anki credentials for justin")) == (
        "the Anki session failed: Missing Anki credentials for justin"
    )
