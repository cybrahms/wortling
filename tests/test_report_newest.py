import json

from wortling.db import Database
from wortling.report import NEWEST_LIMIT, learner_report, newest_words, write_report


def seed(settings, cards):
    """Write one capture per (received_at, cards) pair the caller passes.

    ``cards`` is a list of ``(received_at, lemma, gloss)`` groups: each group is
    one capture, and every card in it has an encounter row for that capture.
    """
    db = Database(settings.data_dir)
    db.initialize(settings)
    user = settings.users[0]
    with db.connect() as conn:
        for index, (received_at, lemmas) in enumerate(cards):
            capture_id = f"capture-{index}"
            conn.execute(
                "INSERT INTO captures(id,user_id,received_at,client_ts,payload_hash) "
                "VALUES (?,?,?,?,?)",
                (capture_id, user.id, received_at, received_at, f"hash-{index}"),
            )
            for lemma, gloss in lemmas:
                card_id = f"card-{index}-{lemma}"
                conn.execute(
                    "INSERT INTO cards(id,user_id,lemma_key,sense_key,fields_json,status,"
                    "updated_at) VALUES (?,?,?,?,?,'added',?)",
                    (
                        card_id,
                        user.id,
                        lemma.lower(),
                        "1",
                        json.dumps({"Lemma": lemma, "Gloss": gloss, "Text": f"… {lemma} …"}),
                        received_at,
                    ),
                )
                conn.execute(
                    "INSERT INTO encounters(card_id,capture_id,sentence) VALUES (?,?,?)",
                    (card_id, capture_id, f"… {lemma} …"),
                )
    return db, user


def test_newest_words_are_ordered_by_the_capture_that_brought_them_in(settings):
    db, user = seed(
        settings,
        [
            ("2026-10-01T09:00:00+00:00", [("der Herbst", "autumn")]),
            ("2026-10-03T09:00:00+00:00", [("die Sitzung", "meeting")]),
            ("2026-10-02T09:00:00+00:00", [("der Rückruf", "callback")]),
        ],
    )
    assert [word["lemma"] for word in newest_words(db, user)] == [
        "die Sitzung",
        "der Rückruf",
        "der Herbst",
    ]


def test_a_card_touched_again_does_not_jump_the_order(settings):
    """The key is the first encounter, not updated_at: editing a card is not collection."""
    db, user = seed(
        settings,
        [
            ("2026-10-01T09:00:00+00:00", [("der Herbst", "autumn")]),
            ("2026-10-02T09:00:00+00:00", [("die Sitzung", "meeting")]),
        ],
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE cards SET updated_at='2026-11-01T00:00:00+00:00' WHERE id='card-0-der Herbst'"
        )
    assert [word["lemma"] for word in newest_words(db, user)] == ["die Sitzung", "der Herbst"]


def test_a_card_seen_much_later_keeps_its_original_place(settings):
    """A word first captured in September stays below one first captured in October.

    The recapture has to be dated after the other card's capture, or a MAX() over
    the encounters would tie them and this test would pass against the wrong key.
    """
    db, user = seed(
        settings,
        [
            ("2026-09-01T09:00:00+00:00", [("der Herbst", "autumn")]),
            ("2026-10-01T09:00:00+00:00", [("die Sitzung", "meeting")]),
        ],
    )
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO captures(id,user_id,received_at,client_ts,payload_hash) "
            "VALUES (?,?,?,?,?)",
            (
                "capture-recapture",
                user.id,
                "2026-11-05T09:00:00+00:00",
                "2026-11-05T09:00:00+00:00",
                "hash-recapture",
            ),
        )
        conn.execute(
            "INSERT INTO encounters(card_id,capture_id,sentence) VALUES (?,?,?)",
            ("card-0-der Herbst", "capture-recapture", "… der Herbst …"),
        )
    assert [word["lemma"] for word in newest_words(db, user)] == ["die Sitzung", "der Herbst"]


def test_dropped_cards_are_left_out_of_the_simplified_list(settings):
    db, user = seed(
        settings,
        [("2026-10-01T09:00:00+00:00", [("der Herbst", "autumn"), ("das Jahr", "year")])],
    )
    with db.connect() as conn:
        conn.execute("UPDATE cards SET status='dropped' WHERE id='card-0-das Jahr'")
    assert [word["lemma"] for word in newest_words(db, user)] == ["der Herbst"]


def test_the_list_is_capped(settings):
    db, user = seed(
        settings,
        [
            (f"2026-10-{day:02d}T09:00:00+00:00", [(f"das Wort{day}", "word")])
            for day in range(1, NEWEST_LIMIT + 6)
        ],
    )
    words = newest_words(db, user)
    assert len(words) == NEWEST_LIMIT
    assert words[0]["lemma"] == f"das Wort{NEWEST_LIMIT + 5}"


def test_the_report_carries_the_list_and_the_gloss(settings):
    db, user = seed(
        settings,
        [
            ("2026-10-01T09:00:00+00:00", [("der Herbst", "autumn")]),
            ("2026-10-02T09:00:00+00:00", [("die Sitzung", "meeting")]),
        ],
    )
    report = learner_report(db, user)
    assert [word["lemma"] for word in report["newest"]] == ["die Sitzung", "der Herbst"]
    assert report["newest"][0]["gloss"] == "meeting"
    assert report["newest"][0]["first_seen"] == "2026-10-02T09:00:00+00:00"


def test_the_markdown_report_opens_with_the_simplified_list(settings):
    db, user = seed(
        settings,
        [
            ("2026-10-01T09:00:00+00:00", [("der Herbst", "autumn")]),
            ("2026-10-02T09:00:00+00:00", [("die Sitzung", "meeting")]),
        ],
    )
    text = write_report(db, user).read_text()
    newest = text.index("## Newest words")
    assert newest < text.index("## Cards") < text.index("## Decisions")
    assert text.index("- die Sitzung — meeting") < text.index("- der Herbst — autumn")
    # The detailed sections are still there, with the cloze text the list omits.
    assert "- added: die Sitzung — meeting" in text


def test_an_empty_collection_still_renders_the_section(settings):
    db, user = seed(settings, [])
    assert newest_words(db, user) == []
    assert "- (none yet)" in write_report(db, user).read_text()
