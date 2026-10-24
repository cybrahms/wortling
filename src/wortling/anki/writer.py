from __future__ import annotations

import html
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from anki.collection import Collection

from wortling.anki.reader import effective_status, open_collection, rebuild_index
from wortling.anki.sync import incremental_sync, login
from wortling.config import Learner, Settings
from wortling.db import Database, now

NOTE_TYPE = "DE Mined v1"
FIELDS = [
    "Key",
    "Text",
    "Lemma",
    "Forms",
    "Gloss",
    "SentenceEN",
    "CEFR",
    "Source",
    "Examples",
    "CaptureId",
]

FRONT = '<main class="context">{{cloze:Text}}</main>'
BACK = (
    FRONT + '<section class="answer"><div class="lemma">{{Lemma}}</div>'
    '<div class="gloss">{{Gloss}}</div>'
    '{{#Forms}}<div class="forms">{{Forms}}</div>{{/Forms}}</section>'
    '{{#SentenceEN}}<section class="translation">{{SentenceEN}}</section>{{/SentenceEN}}'
    '{{#Source}}<footer class="source">{{Source}}</footer>{{/Source}}'
    '{{#Examples}}<details class="examples"><summary>Other encounters</summary>'
    "<div>{{Examples}}</div></details>{{/Examples}}"
)
CARD_CSS = """
.card { font-family: -apple-system, BlinkMacSystemFont, Arial, sans-serif;
  font-size: 20px; line-height: 1.5; text-align: left;
  color: #20272d; background: #fff; margin: 0; padding: 24px 20px; }
.context, .answer, .translation, .source, .examples { max-width: 42rem;
  margin-left: auto; margin-right: auto; overflow-wrap: anywhere; }
.cloze { font-weight: 600; color: #146c60; }
.answer { margin-top: 24px; padding-top: 18px; border-top: 1px solid #d9dfe5; }
.lemma { font-size: 22px; font-weight: 600; }
.gloss { margin-top: 4px; }
.forms { margin-top: 8px; font-size: 15px; color: #536171; }
.translation { margin-top: 20px; font-size: 16px; color: #536171; }
.source { margin-top: 18px; font-size: 13px; color: #626e7c; }
.examples { margin-top: 16px; font-size: 15px; color: #536171; }
.examples summary { cursor: pointer; }
.examples div { margin-top: 8px; }
.nightMode.card, .night_mode.card { color: #edf1f5; background: #181a1d; }
.nightMode .cloze, .night_mode .cloze { color: #7cd9c7; }
.nightMode .answer, .night_mode .answer { border-color: #41464e; }
.nightMode .forms, .nightMode .translation, .nightMode .source, .nightMode .examples,
.night_mode .forms, .night_mode .translation, .night_mode .source, .night_mode .examples
  { color: #bac4cf; }
@media (max-width: 600px) { .card { font-size: 19px; padding: 20px 18px; }
  .lemma { font-size: 21px; } }
""".strip()


def ensure_note_type(collection: Collection):
    model = collection.models.by_name(NOTE_TYPE)
    if model:
        if [field["name"] for field in model["flds"]] != FIELDS or model["type"] != 1:
            raise RuntimeError("DE Mined v1 schema differs; human migration is required")
        if len(model["tmpls"]) != 1:
            raise RuntimeError("DE Mined v1 templates differ; human migration is required")
        template = model["tmpls"][0]
        if (template["qfmt"], template["afmt"], model["css"]) != (FRONT, BACK, CARD_CSS):
            template["qfmt"], template["afmt"], model["css"] = FRONT, BACK, CARD_CSS
            collection.models.update_dict(model)
            model = collection.models.by_name(NOTE_TYPE)
        return model
    model = collection.models.new(NOTE_TYPE)
    model["type"] = 1  # Anki's cloze note type, verified against built-in Cloze.
    for name in FIELDS:
        collection.models.add_field(model, collection.models.new_field(name))
    template = collection.models.new_template("Context")
    template["qfmt"] = FRONT
    template["afmt"] = BACK
    collection.models.add_template(model, template)
    model["css"] = CARD_CSS
    collection.models.add(model)
    return collection.models.by_name(NOTE_TYPE)


def backup_collection(collection: Collection, data_dir: Path, user_id: str) -> Path:
    folder = data_dir / "backups" / "collections" / user_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f"{time.time_ns()}.anki2"
    # SQLite online backup includes committed WAL contents while the collection is open.
    with (
        closing(sqlite3.connect(collection.path)) as source,
        closing(sqlite3.connect(path)) as target,
    ):
        source.backup(target)
    path.chmod(0o600)
    for old in sorted(folder.glob("*.anki2"))[:-7]:
        old.unlink()
    return path


def owned_notes(collection: Collection) -> dict[str, int]:
    model = collection.models.by_name(NOTE_TYPE)
    if model is None:
        return {}
    result = {}
    for nid in collection.db.list("SELECT id FROM notes WHERE mid=?", model["id"]):
        note = collection.get_note(nid)
        key = html.unescape(note["Key"])
        if key in result:
            raise RuntimeError("Duplicate robot keys in collection; refusing ambiguous updates")
        result[key] = nid
    return result


def promote_new(collection: Collection, note_ids: list[int]) -> list[int]:
    card_ids = []
    for nid in dict.fromkeys(note_ids):
        card_ids.extend(
            collection.db.list(
                "SELECT id FROM cards WHERE nid=? AND type=0 AND queue=0 AND reps=0 "
                "ORDER BY ord,id",
                nid,
            )
        )
    if card_ids:
        selected = set(card_ids)
        remaining = [
            cid
            for cid in collection.db.list(
                "SELECT id FROM cards WHERE type=0 AND queue=0 AND reps=0 ORDER BY due,id"
            )
            if cid not in selected
        ]
        # Native shift_existing also shifts suspended new cards. Include only
        # active unseen cards and preserve their relative order explicitly.
        collection.sched.reposition_new_cards(
            card_ids + remaining,
            starting_from=0,
            step_size=1,
            randomize=False,
            shift_existing=False,
        )
    return card_ids


def write_session(settings: Settings, db: Database, user: Learner, *, force: bool = False) -> dict:
    if not user.writes_enabled or not user.dry_run_approved:
        raise RuntimeError("Writes require writes_enabled and human-approved dry-run output")
    session_id = str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO write_sessions(id,user_id,started_at,status) VALUES (?,?,?,'running')",
            (session_id, user.id, now()),
        )
    count = 0
    try:
        with open_collection(settings.data_dir, user.id) as collection:
            backup = backup_collection(collection, settings.data_dir, user.id)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE write_sessions SET backup_path=? WHERE id=?", (str(backup), session_id)
                )
            auth = login(collection, user, settings.anki_sync_endpoint)
            incremental_sync(collection, auth)
            rebuild_index(db, user, collection)
            owned = owned_notes(collection)
            blocked = False
            with db.connect() as conn:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM cards WHERE user_id=? AND status IN ('pending','updated'"
                        + (",'blocked'" if force else "")
                        + ") ORDER BY updated_at,id",
                        (user.id,),
                    )
                ]
                additions, updates = [], []
                for row in rows:
                    fields = json.loads(row["fields_json"])
                    expected_key = row["lemma_key"] + ":" + row["sense_key"]
                    if fields["Key"] != expected_key or set(fields) != set(FIELDS):
                        raise RuntimeError("Invalid queued note fields")
                    existing_id = owned.get(expected_key)
                    if row["anki_note_id"] and existing_id != row["anki_note_id"]:
                        raise RuntimeError("Owned note missing or changed; refusing to recreate it")
                    if existing_id:
                        # Recover a crash between Anki add and SQLite note-id recording.
                        row["recovered"] = row["anki_note_id"] is None
                        row["anki_note_id"] = existing_id
                        updates.append(row)
                        continue
                    matches = [
                        dict(match)
                        for match in conn.execute(
                            "SELECT * FROM known_index WHERE user_id=? AND lemma_key=?",
                            (user.id, row["lemma_key"]),
                        )
                    ]
                    if effective_status(matches):
                        conn.execute(
                            "UPDATE cards SET status='dropped',updated_at=? WHERE id=?",
                            (now(), row["id"]),
                        )
                    else:
                        additions.append(row)
                if len(additions) > user.max_adds_per_run and not force:
                    conn.executemany(
                        "UPDATE cards SET status='blocked',updated_at=? WHERE id=?",
                        [(now(), row["id"]) for row in additions],
                    )
                    conn.execute(
                        "INSERT INTO alerts(user_id,kind,message,created_at) VALUES (?,?,?,?)",
                        (
                            user.id,
                            "circuit_breaker",
                            f"Blocked {len(additions)} additions; review before write --force",
                            now(),
                        ),
                    )
                    conn.execute(
                        "UPDATE write_sessions SET status='blocked',finished_at=? WHERE id=?",
                        (now(), session_id),
                    )
                    blocked = True
            if blocked:
                incremental_sync(collection, auth)
                return {"session_id": session_id, "status": "blocked", "added": 0}
            model = ensure_note_type(collection) if additions or updates else None
            deck_id = collection.decks.id(user.target_deck) if additions else None
            promoted = []
            for row in updates + additions:
                fields = json.loads(row["fields_json"])
                if row["anki_note_id"]:
                    note = collection.get_note(row["anki_note_id"])
                    if note.note_type()["name"] != NOTE_TYPE or note["Key"] != fields["Key"]:
                        raise RuntimeError("Refusing to update a note not owned by Wortling")
                else:
                    note = collection.new_note(model)
                for name in FIELDS:
                    note[name] = fields[name]
                tags = [
                    "mined",
                    "cefr::" + fields["CEFR"],
                    "domain::" + html.unescape(fields["Source"]).split(" · ")[0],
                    "capture::" + fields["CaptureId"],
                ]
                note.tags = sorted(set(note.tags) | set(tags))
                if row["anki_note_id"]:
                    collection.update_note(note)
                    if row.get("recovered"):
                        promoted.append(note.id)
                else:
                    collection.add_note(note, deck_id)
                    promoted.append(note.id)
                    count += 1
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE cards SET anki_note_id=?,status='added',"
                        "added_at=COALESCE(added_at,?),updated_at=? WHERE id=?",
                        (note.id, now(), now(), row["id"]),
                    )
            with db.connect() as conn:
                requested = [
                    row[0]
                    for row in conn.execute(
                        "SELECT note_id FROM promotions WHERE user_id=? ORDER BY requested_at DESC",
                        (user.id,),
                    )
                ]
            if user.promote_new_cards:
                promote_new(collection, promoted + requested)
            incremental_sync(collection, auth)
            rebuild_index(db, user, collection)
            with db.connect() as conn:
                conn.execute("DELETE FROM promotions WHERE user_id=?", (user.id,))
                conn.execute(
                    "UPDATE write_sessions SET status='complete',finished_at=?,"
                    "added_count=? WHERE id=?",
                    (now(), count, session_id),
                )
        return {"session_id": session_id, "status": "complete", "added": count}
    except Exception as exc:
        with db.connect() as conn:
            conn.execute(
                "UPDATE write_sessions SET status='error',finished_at=?,error=?,"
                "added_count=? WHERE id=?",
                (now(), str(exc)[:1000], count, session_id),
            )
            conn.execute(
                "INSERT INTO alerts(user_id,kind,message,created_at) VALUES (?,?,?,?)",
                (user.id, "write_failed", str(exc)[:1000], now()),
            )
        raise
