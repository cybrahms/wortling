"""Real Anki backend and sync server. No test account can reach AnkiWeb."""

import json
import os
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest
from anki.collection import Collection
from anki.scheduler.v3 import CardAnswer

from wortling.anki.reader import open_collection
from wortling.anki.sync import FullSyncRequired, bootstrap_download, incremental_sync
from wortling.anki.writer import backup_collection, write_session
from wortling.db import Database, now


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


def ready(settings, monkeypatch, sync_endpoint):
    user = settings.users[0]
    monkeypatch.setenv(user.ankiweb_user_env, "test")
    monkeypatch.setenv(user.ankiweb_pass_env, "test")
    settings.anki_sync_endpoint = sync_endpoint
    user.processing_enabled = True
    user.dry_run_approved = True
    user.writes_enabled = True
    db = Database(settings.data_dir)
    db.initialize(settings)
    with open_collection(settings.data_dir, user.id, create=True) as collection:
        auth = collection.sync_login("test", "test", sync_endpoint)
        bootstrap_download(collection, auth)
    return db, user


def pending(db, user, word="Sitzung"):
    card_id = str(uuid.uuid4())
    fields = {
        "Key": word.casefold() + ":meeting",
        "Text": "Die {{c1::" + word + "::meeting}} beginnt.",
        "Lemma": "die " + word + ", -en",
        "Forms": "",
        "Gloss": "meeting",
        "SentenceEN": "The meeting starts.",
        "CEFR": "B1",
        "Source": "school · 2026-10-14",
        "Examples": "",
        "CaptureId": str(uuid.uuid4()),
    }
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO cards(id,user_id,lemma_key,sense_key,fields_json,status,updated_at) "
            "VALUES (?,?,?,?,?,'pending',?)",
            (card_id, user.id, word.casefold(), "meeting", json.dumps(fields), now()),
        )
    return card_id


def test_write_second_client_review_and_field_update(
    settings, monkeypatch, sync_endpoint, tmp_path
):
    db, user = ready(settings, monkeypatch, sync_endpoint)
    card_id = pending(db, user)
    assert write_session(settings, db, user)["added"] == 1
    phone = Collection(str(tmp_path / "phone.anki2"))
    try:
        auth = phone.sync_login("test", "test", sync_endpoint)
        bootstrap_download(phone, auth)
        nid = phone.db.scalar(
            "SELECT id FROM notes WHERE mid IN (SELECT id FROM notetypes WHERE name='DE Mined v1')"
        )
        card = phone.get_card(phone.find_cards(f"nid:{nid}")[0])
        card.start_timer()
        states = phone._backend.get_scheduling_states(card.id)
        answer = phone.sched.build_answer(card=card, states=states, rating=CardAnswer.GOOD)
        phone.sched.answer_card(answer)
        incremental_sync(phone, auth)
        review = phone.db.all("SELECT id,cid,ease,ivl,lastIvl FROM revlog")
        schedule = phone.db.all(
            "SELECT id,queue,type,due,ivl,reps,lapses FROM cards WHERE nid=?", nid
        )
        with db.connect() as conn:
            row = conn.execute("SELECT fields_json FROM cards WHERE id=?", (card_id,)).fetchone()
            fields = json.loads(row[0])
            fields["Examples"] = "Eine weitere Sitzung."
            conn.execute(
                "UPDATE cards SET fields_json=?,status='updated' WHERE id=?",
                (json.dumps(fields), card_id),
            )
            conn.execute("INSERT INTO promotions VALUES (?,?,?)", (user.id, nid, now()))
        assert write_session(settings, db, user)["added"] == 0
        incremental_sync(phone, auth)
        assert phone.db.all("SELECT id,cid,ease,ivl,lastIvl FROM revlog") == review
        assert (
            phone.db.all("SELECT id,queue,type,due,ivl,reps,lapses FROM cards WHERE nid=?", nid)
            == schedule
        )
        assert phone.get_note(nid)["Examples"] == "Eine weitere Sitzung."
        assert phone.db.scalar("SELECT COUNT(*) FROM cards WHERE nid=?", nid) == 1
    finally:
        phone.close()


def test_circuit_breaker_adds_nothing_and_force_is_explicit(settings, monkeypatch, sync_endpoint):
    db, user = ready(settings, monkeypatch, sync_endpoint)
    user.max_adds_per_run = 1
    pending(db, user, "Sitzung")
    pending(db, user, "Antrag")
    result = write_session(settings, db, user)
    assert result["status"] == "blocked" and result["added"] == 0
    with open_collection(settings.data_dir, user.id) as collection:
        assert collection.db.scalar("SELECT COUNT(*) FROM notes") == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cards WHERE status='blocked'").fetchone()[0] == 2
    assert write_session(settings, db, user, force=True)["added"] == 2


def test_full_sync_aborts_before_add(settings, monkeypatch, sync_endpoint):
    db, user = ready(settings, monkeypatch, sync_endpoint)
    pending(db, user)
    with open_collection(settings.data_dir, user.id) as collection:
        collection.mod_schema(check=False)
    with pytest.raises(FullSyncRequired):
        write_session(settings, db, user)
    with open_collection(settings.data_dir, user.id) as collection:
        assert collection.db.scalar("SELECT COUNT(*) FROM notes") == 0
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM cards").fetchone()[0] == "pending"
        assert (
            conn.execute("SELECT COUNT(*) FROM alerts WHERE kind='write_failed'").fetchone()[0] == 1
        )


def test_backup_retains_last_seven(settings, monkeypatch, sync_endpoint):
    db, user = ready(settings, monkeypatch, sync_endpoint)
    with open_collection(settings.data_dir, user.id) as collection:
        for _ in range(9):
            backup_collection(collection, settings.data_dir, user.id)
    assert len(list((settings.data_dir / "backups" / "collections" / user.id).glob("*.anki2"))) == 7


def test_promotions_only_reposition_unseen_active_cards(settings, monkeypatch, sync_endpoint):
    from wortling.anki.writer import promote_new

    db, user = ready(settings, monkeypatch, sync_endpoint)
    with open_collection(settings.data_dir, user.id) as collection:
        deck = collection.decks.id("Existing")
        notes = []
        for word in ["queued", "suspended", "review"]:
            note = collection.new_note(collection.models.by_name("Basic"))
            note["Front"] = word
            collection.add_note(note, deck)
            notes.append(note.id)
        # Fixture scheduling, outside application code.
        collection.db.execute("UPDATE cards SET due=100 WHERE nid=?", notes[0])
        collection.db.execute("UPDATE cards SET queue=-1,due=80 WHERE nid=?", notes[1])
        collection.db.execute(
            "UPDATE cards SET type=2,queue=2,reps=5,ivl=30,due=7 WHERE nid=?", notes[2]
        )
        unchanged = collection.db.all(
            "SELECT id,type,queue,due,reps,ivl FROM cards WHERE nid IN (?,?)", notes[1], notes[2]
        )
        promoted = promote_new(collection, notes)
        assert len(promoted) == 1
        assert collection.db.scalar("SELECT due FROM cards WHERE nid=?", notes[0]) == 0
        assert (
            collection.db.all(
                "SELECT id,type,queue,due,reps,ivl FROM cards WHERE nid IN (?,?)",
                notes[1],
                notes[2],
            )
            == unchanged
        )


def test_promotion_puts_encountered_note_before_older_new_notes(
    settings, monkeypatch, sync_endpoint
):
    from wortling.anki.writer import promote_new

    _, user = ready(settings, monkeypatch, sync_endpoint)
    with open_collection(settings.data_dir, user.id) as collection:
        nids = []
        for word in ["old-one", "old-two", "encountered"]:
            note = collection.new_note(collection.models.by_name("Basic"))
            note["Front"] = word
            collection.add_note(note, collection.decks.id("Existing"))
            nids.append(note.id)
        promote_new(collection, [nids[2]])
        assert collection.db.list("SELECT nid FROM cards ORDER BY due,id") == [nids[2], *nids[:2]]


def test_write_recovers_note_added_before_receipt_was_recorded(
    settings, monkeypatch, sync_endpoint
):
    from wortling.anki.writer import ensure_note_type

    db, user = ready(settings, monkeypatch, sync_endpoint)
    card_id = pending(db, user)
    with db.connect() as conn:
        fields = json.loads(
            conn.execute("SELECT fields_json FROM cards WHERE id=?", (card_id,)).fetchone()[0]
        )
    with open_collection(settings.data_dir, user.id) as collection:
        note = collection.new_note(ensure_note_type(collection))
        for name, value in fields.items():
            note[name] = value
        collection.add_note(note, collection.decks.id(user.target_deck))
        nid = note.id
        original_card = collection.find_cards(f"nid:{nid}")[0]
        incremental_sync(collection, collection.sync_login("test", "test", sync_endpoint))
    result = write_session(settings, db, user)
    assert result["added"] == 0
    with open_collection(settings.data_dir, user.id) as collection:
        assert collection.db.scalar("SELECT COUNT(*) FROM notes") == 1
        assert collection.find_cards(f"nid:{nid}") == [original_card]
        assert collection.db.scalar("SELECT due FROM cards WHERE nid=?", nid) == 0
    with db.connect() as conn:
        assert (
            conn.execute("SELECT anki_note_id FROM cards WHERE id=?", (card_id,)).fetchone()[0]
            == nid
        )


def test_independent_mined_deck_preserves_all_existing_scheduling(
    settings, monkeypatch, sync_endpoint
):
    db, user = ready(settings, monkeypatch, sync_endpoint)
    user.target_deck = "Wortling"
    user.promote_new_cards = False
    with open_collection(settings.data_dir, user.id) as collection:
        existing = []
        for word in ["unseen", "reviewed"]:
            note = collection.new_note(collection.models.by_name("Basic"))
            note["Front"] = word
            collection.add_note(note, collection.decks.id("Existing"))
            existing.append(note.id)
        collection.db.execute("UPDATE cards SET due=100 WHERE nid=?", existing[0])
        collection.db.execute(
            "UPDATE cards SET type=2,queue=2,reps=5,ivl=30,due=7 WHERE nid=?", existing[1]
        )
        # Fixture creation only. Bootstrap and application paths never full-upload.
        incremental_sync(collection, collection.sync_login("test", "test", sync_endpoint))
        before = collection.db.all(
            "SELECT id,nid,did,type,queue,due,ivl,reps,lapses,factor,left,odue,odid "
            "FROM cards ORDER BY id"
        )
    with db.connect() as conn:
        conn.execute("INSERT INTO promotions VALUES (?,?,?)", (user.id, existing[0], now()))
    pending(db, user)
    assert write_session(settings, db, user)["added"] == 1
    with open_collection(settings.data_dir, user.id) as collection:
        after = collection.db.all(
            "SELECT id,nid,did,type,queue,due,ivl,reps,lapses,factor,left,odue,odid "
            "FROM cards WHERE nid IN (?,?) ORDER BY id",
            *existing,
        )
        assert after == before
        deck = collection.decks.id("Wortling", create=False)
        assert collection.db.scalar("SELECT COUNT(*) FROM cards WHERE did=?", deck) == 1


def test_write_backs_up_the_collection_before_rewriting_the_template(
    settings, monkeypatch, sync_endpoint, tmp_path
):
    from wortling.anki.writer import AFMT, NOTE_TYPE

    db, user = ready(settings, monkeypatch, sync_endpoint)
    pending(db, user, "Sitzung")
    assert write_session(settings, db, user)["added"] == 1
    old_afmt = "{{cloze:Text}}<hr><b>{{Lemma}}</b><br>{{Forms}}"
    with open_collection(settings.data_dir, user.id) as collection:
        model = collection.models.by_name(NOTE_TYPE)
        model["tmpls"][0]["afmt"] = old_afmt
        collection.models.save(model)

    pending(db, user, "Antrag")
    assert write_session(settings, db, user)["added"] == 1

    backups = sorted((settings.data_dir / "backups" / "collections" / user.id).glob("*.anki2"))
    assert backups
    copy = tmp_path / "before-the-rewrite.anki2"
    copy.write_bytes(backups[-1].read_bytes())
    before = Collection(str(copy))
    try:
        assert before.models.by_name(NOTE_TYPE)["tmpls"][0]["afmt"] == old_afmt
    finally:
        before.close()
    with open_collection(settings.data_dir, user.id) as collection:
        assert collection.models.by_name(NOTE_TYPE)["tmpls"][0]["afmt"] == AFMT
