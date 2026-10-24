import json

import pytest
from fastapi.testclient import TestClient

from wortling.api import create_app
from wortling.backup import backup_all, verify_backup
from wortling.db import Database
from wortling.reprocess import reprocess


def test_backup_checksums_detect_corruption(settings):
    db = Database(settings.data_dir)
    db.initialize(settings)
    path = backup_all(settings, db)
    assert verify_backup(path) == 2
    with (path / "wortling.sqlite3").open("ab") as stream:
        stream.write(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        verify_backup(path)


def test_report_and_image_auth(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert "Wortling" in client.get("/").text
        assert client.get("/v1/report").status_code == 401
        response = client.get("/v1/report", headers={"Authorization": "Bearer " + "a" * 48})
        assert response.status_code == 200
        assert response.json()["learner"] == "Test"
        assert response.json()["writes_enabled"] is False
        assert "token_hash" not in json.dumps(response.json())
        assert "a" * 48 not in json.dumps(response.json())


def test_report_exposes_saved_capture_job_status(settings):
    from test_pipeline import capture

    capture_id = capture(settings, "Die Sitzung.")
    with TestClient(create_app(settings)) as client:
        report = client.get("/v1/report", headers={"Authorization": "Bearer " + "a" * 48}).json()
        assert report["captures"][0]["id"] == capture_id
        assert report["captures"][0]["processing_status"] == "pending"
        assert report["captures"][0]["status"] == "received"


def test_reprocess_changes_owned_fields_without_new_card(settings):
    settings.llm.prompt_versions["extract"] = "v4"
    from test_pipeline import RecordedClient, capture

    from wortling.pipeline.engine import Pipeline

    capture_id = capture(settings, "Die Sitzung beginnt morgen.")
    db = Database(settings.data_dir)
    client = RecordedClient()
    pipeline = Pipeline(settings, db, client)
    pipeline.process(capture_id)
    with db.connect() as conn:
        original = dict(conn.execute("SELECT * FROM cards").fetchone())
        conn.execute("UPDATE cards SET anki_note_id=123,status='added'")
    assert reprocess(db, "test", "extract.v4") == 1
    pipeline.process(capture_id)
    with db.connect() as conn:
        cards = conn.execute("SELECT * FROM cards").fetchall()
        assert len(cards) == 1
        assert cards[0]["id"] == original["id"]
        assert cards[0]["anki_note_id"] == 123
        assert cards[0]["status"] == "updated"


def test_reprocess_removes_obsolete_pending_sense(settings):
    settings.llm.prompt_versions["extract"] = "v4"
    from test_pipeline import RecordedClient, capture

    from wortling.pipeline.engine import Pipeline
    from wortling.reprocess import reprocess

    capture_id = capture(settings, "Die Sitzung beginnt morgen.")
    db = Database(settings.data_dir)
    client = RecordedClient()
    Pipeline(settings, db, client).process(capture_id)
    with db.connect() as conn:
        conn.execute("UPDATE cards SET sense_key='obsolete'")
    assert reprocess(db, "test", "extract.v4") == 1
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM cards").fetchone()[0] == "dropped"
        # A failed/retried job preserves the reprocessing intent.
        conn.execute("UPDATE captures SET status='error'")
    Pipeline(settings, db, client).process(capture_id)
    with db.connect() as conn:
        statuses = {row[0]: row[1] for row in conn.execute("SELECT sense_key,status FROM cards")}
        assert statuses == {"obsolete": "dropped", "meeting": "pending"}
        assert conn.execute("SELECT reprocess FROM jobs").fetchone()[0] == 0


def test_targeted_explicit_lookup_survives_retry_and_skips_judge(settings):
    from test_pipeline import RecordedClient, capture

    from wortling.pipeline.engine import Pipeline

    first = capture(settings, "Die Sitzung beginnt morgen.")
    second = capture(settings, "Die Sitzung ist wichtig.")
    db = Database(settings.data_dir)
    assert reprocess(db, "test", capture_id=first, explicit_lookup=True) == 1
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT explicit_lookup FROM jobs WHERE capture_id=?", (second,)
            ).fetchone()[0]
            == 0
        )
        conn.execute("UPDATE captures SET status='error' WHERE id=?", (first,))
    client = RecordedClient()
    Pipeline(settings, db, client).process(first)
    assert client.stages == ["normalize", "extract", "generate"]
    with db.connect() as conn:
        decision = conn.execute(
            "SELECT outcome,reason FROM decisions WHERE stage='judge'"
        ).fetchone()
        assert decision["outcome"] == "explicit_lookup"
        assert "Learner explicitly requested" in decision["reason"]
        assert conn.execute("SELECT text FROM captures WHERE id=?", (first,)).fetchone()[0] == (
            "Die Sitzung beginnt morgen."
        )
    with pytest.raises(ValueError, match="requires one"):
        reprocess(db, "test", explicit_lookup=True)
    with pytest.raises(ValueError, match="not found"):
        reprocess(db, "someone_else", capture_id=first, explicit_lookup=True)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE capture_id=?", (first,))
    with pytest.raises(ValueError, match="still processing"):
        reprocess(db, "test", capture_id=first, explicit_lookup=True)
