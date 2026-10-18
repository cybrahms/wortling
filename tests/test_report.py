import sqlite3
from importlib.resources import files

from wortling.db import Database
from wortling.report import learner_report


def test_cards_stay_in_creation_order_after_updates_and_anki_writes(settings):
    db = Database(settings.data_dir)
    db.initialize(settings)
    with db.connect() as conn:
        for card_id, timestamp in [
            ("old", "2026-10-17T10:00:00+00:00"),
            ("new", "2026-10-18T10:00:00+00:00"),
        ]:
            conn.execute(
                "INSERT INTO cards(id,user_id,lemma_key,sense_key,fields_json,status,updated_at) "
                "VALUES (?, 'test', ?, 'sense', '{}', 'pending', ?)",
                (card_id, card_id, timestamp),
            )
        conn.execute(
            "UPDATE cards SET status='added', added_at=?, updated_at=? WHERE id='old'",
            ("2026-10-19T10:00:00+00:00", "2026-10-19T10:00:00+00:00"),
        )
    cards = learner_report(db, settings.users[0])["cards"]
    assert [card["id"] for card in cards] == ["new", "old"]
    assert cards[1]["created_at"] == "2026-10-17T10:00:00+00:00"


def test_creation_time_migration_uses_first_capture_and_preserves_legacy_cards():
    migrations = files("wortling").joinpath("migrations")
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(migrations.joinpath("001_initial.sql").read_text())
        conn.execute("INSERT INTO users(id,name) VALUES ('test','Test')")
        for card_id, added_at in [("captured", None), ("written", "2026-10-02"), ("queued", None)]:
            conn.execute(
                "INSERT INTO cards(id,user_id,lemma_key,sense_key,fields_json,status,"
                "added_at,updated_at) VALUES (?, 'test', ?, 'sense', '{}', 'pending', ?, ?)",
                (card_id, card_id, added_at, "2026-10-18"),
            )
        for capture_id, timestamp in [("first", "2026-10-01"), ("recapture", "2026-10-17")]:
            conn.execute(
                "INSERT INTO captures(id,user_id,received_at,client_ts,payload_hash) "
                "VALUES (?, 'test', ?, ?, ?)",
                (capture_id, timestamp, timestamp, capture_id),
            )
            conn.execute("INSERT INTO encounters VALUES ('captured', ?, 'example')", (capture_id,))
        conn.executescript(migrations.joinpath("008_card_created_at.sql").read_text())
        assert dict(conn.execute("SELECT id,created_at FROM cards")) == {
            "captured": "2026-10-01",
            "written": "2026-10-02",
            "queued": "2026-10-18",
        }
