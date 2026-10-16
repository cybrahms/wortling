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
