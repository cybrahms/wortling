from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from anki.collection import Collection
from filelock import FileLock

from wortling.config import Learner
from wortling.db import Database, now
from wortling.lemma import lemma_key


@contextmanager
def open_collection(data_dir: Path, user_id: str, *, create: bool = False) -> Iterator[Collection]:
    folder = data_dir / "collections" / user_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(folder / "collection.lock", timeout=30):
        path = folder / "collection.anki2"
        if not create and not path.exists():
            raise RuntimeError(f"Collection for {user_id} requires download-only bootstrap")
        collection = Collection(str(path.resolve()))
        try:
            yield collection
        finally:
            collection.close()
        path.chmod(0o600)


def note_status(cards: list[dict]) -> str:
    active = [card for card in cards if card["queue"] != -1]
    if not active:
        return "suspended"
    if any(card["reps"] > 0 and card["ivl"] >= 21 for card in active):
        return "known"
    if any(card["reps"] > 0 for card in active):
        return "learning"
    return "queued"


def effective_status(matches: list[dict]) -> str | None:
    statuses = {row["status"] for row in matches}
    # Any suspended note is an explicit opt-out unless an active learned card exists.
    return next(
        (status for status in ["known", "learning", "suspended", "queued"] if status in statuses),
        None,
    )


def inspect_index(collection: Collection, limit: int = 3) -> list[dict]:
    decks = {deck.id: deck.name for deck in collection.decks.all_names_and_ids()}
    samples = defaultdict(list)
    for deck_id, note_id in collection.db.all(
        "SELECT DISTINCT did,nid FROM cards ORDER BY did,nid"
    ):
        name = decks.get(deck_id, str(deck_id))
        if len(samples[name]) >= limit:
            continue
        note = collection.get_note(note_id)
        samples[name].append(
            {
                "note_id": note_id,
                "note_type": note.note_type()["name"],
                "fields": dict(note.items()),
            }
        )
    return [{"deck": deck, "samples": rows} for deck, rows in samples.items()]


def rebuild_index(db: Database, user: Learner, collection: Collection) -> int:
    mappings = {mapping.deck: mapping for mapping in user.known_decks}
    decks = {deck.id: deck.name for deck in collection.decks.all_names_and_ids()}
    by_note = defaultdict(list)
    for nid, did, odid, queue, reps, ivl in collection.db.all(
        "SELECT nid,did,odid,queue,reps,ivl FROM cards"
    ):
        by_note[nid].append(
            {"deck": decks.get(odid or did, ""), "queue": queue, "reps": reps, "ivl": ivl}
        )
    entries = []
    owned_keys = set()
    timestamp = now()
    for nid, cards in by_note.items():
        note = collection.get_note(nid)
        owned = note.note_type()["name"].startswith("DE Mined v") and "Key" in note
        if owned:
            owned_keys.add(note["Key"])
        candidates = []
        for deck_name in {card["deck"] for card in cards}:
            mapping = mappings.get(deck_name)
            # Parent deck configuration covers its children.
            if mapping is None:
                mapping = next(
                    (m for name, m in mappings.items() if deck_name.startswith(name + "::")), None
                )
            if mapping:
                if mapping.lemma_field not in note:
                    raise ValueError(f"Missing lemma field {mapping.lemma_field!r} in {deck_name}")
                rank = None
                if mapping.rank_field:
                    if mapping.rank_field not in note:
                        raise ValueError(
                            f"Missing rank field {mapping.rank_field!r} in {deck_name}"
                        )
                    try:
                        rank = int(note[mapping.rank_field])
                    except ValueError:
                        pass
                candidates.append((lemma_key(note[mapping.lemma_field]), deck_name, rank))
            elif owned:
                candidates.append((note["Key"].partition(":")[0], deck_name, None))
        for key, deck_name, rank in candidates:
            if key:
                entries.append((user.id, key, note_status(cards), deck_name, nid, rank, timestamp))
    with db.connect() as conn:
        conn.execute("DELETE FROM known_index WHERE user_id=?", (user.id,))
        conn.executemany("INSERT OR REPLACE INTO known_index VALUES (?,?,?,?,?,?,?)", entries)
        pending = conn.execute(
            "SELECT id,fields_json FROM cards WHERE user_id=? AND anki_note_id IS NULL "
            "AND status IN ('pending','blocked') AND EXISTS (SELECT 1 FROM known_index k "
            "WHERE k.user_id=cards.user_id AND k.lemma_key=cards.lemma_key)",
            (user.id,),
        ).fetchall()
        for card in pending:
            fields = json.loads(card["fields_json"])
            if fields["Key"] not in owned_keys:
                conn.execute(
                    "UPDATE cards SET status='dropped',updated_at=? WHERE id=?",
                    (timestamp, card["id"]),
                )
                run = conn.execute(
                    "SELECT id FROM processing_runs WHERE capture_id=? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (fields["CaptureId"],),
                ).fetchone()
                if run:
                    conn.execute(
                        "INSERT INTO decisions(run_id,stage,outcome,reason,"
                        "prompt_version,created_at) "
                        "VALUES (?,'known','dropped','Present after collection index refresh',"
                        "'code.v1',?)",
                        (run[0], timestamp),
                    )
    return len(entries)
