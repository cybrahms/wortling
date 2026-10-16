import pytest

from wortling.anki.reader import effective_status, note_status
from wortling.anki.sync import check_endpoint


@pytest.mark.parametrize(
    "cards,expected",
    [
        ([{"queue": 0, "reps": 0, "ivl": 0}], "queued"),
        ([{"queue": 2, "reps": 3, "ivl": 20}], "learning"),
        ([{"queue": 2, "reps": 3, "ivl": 21}], "known"),
        ([{"queue": -1, "reps": 3, "ivl": 100}], "suspended"),
        ([{"queue": -2, "reps": 3, "ivl": 100}], "known"),
    ],
)
def test_status(cards, expected):
    assert note_status(cards) == expected


def test_suspension_precedence():
    assert effective_status([{"status": "queued"}, {"status": "suspended"}]) == "suspended"
    assert effective_status([{"status": "known"}, {"status": "suspended"}]) == "known"


def test_real_access_requires_approval():
    for endpoint in [None, "https://sync.ankiweb.net", "http://evil.example/"]:
        with pytest.raises(RuntimeError, match="confirmation"):
            check_endpoint(endpoint, False)
    check_endpoint("http://127.0.0.1:8082/", False)


def test_bootstrap_resolves_shard_before_download():
    from types import SimpleNamespace

    from anki.sync_pb2 import SyncAuth, SyncCollectionResponse

    from wortling.anki.sync import bootstrap_download

    calls = []
    auth = SyncAuth(hkey="test-only")
    collection = SimpleNamespace(
        db=SimpleNamespace(scalar=lambda _: 0),
        sync_collection=lambda *args, **kwargs: SyncCollectionResponse(
            required=SyncCollectionResponse.FULL_DOWNLOAD,
            new_endpoint="http://127.0.0.1:12345/",
        ),
        close_for_full_sync=lambda: calls.append("close"),
        full_upload_or_download=lambda **kwargs: calls.append(
            (kwargs["auth"].endpoint, kwargs["upload"])
        ),
        reopen=lambda **kwargs: calls.append("reopen"),
    )
    bootstrap_download(collection, auth)
    assert calls == ["close", ("http://127.0.0.1:12345/", False), "reopen"]


def test_index_refresh_drops_newly_known_pending_notes(settings):
    from anki.collection import Collection
    from test_anki_sync import pending

    from wortling.anki.reader import rebuild_index
    from wortling.config import KnownDeck
    from wortling.db import Database

    db = Database(settings.data_dir)
    db.initialize(settings)
    user = settings.users[0]
    user.known_decks = [KnownDeck(deck="Known", lemma_field="Front")]
    pending(db, user)
    collection = Collection(str(settings.data_dir / "fixture.anki2"))
    try:
        note = collection.new_note(collection.models.by_name("Basic"))
        note["Front"] = "die Sitzung, -en"
        collection.add_note(note, collection.decks.id("Known"))
        rebuild_index(db, user, collection)
        with db.connect() as conn:
            assert conn.execute("SELECT status FROM cards").fetchone()[0] == "dropped"
    finally:
        collection.close()
