CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT UNIQUE
);
CREATE TABLE captures (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    received_at TEXT NOT NULL,
    text TEXT,
    url TEXT,
    image_path TEXT,
    image_mime TEXT,
    client_ts TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    error TEXT,
    UNIQUE(user_id, idempotency_key)
);
CREATE TRIGGER captures_raw_immutable
BEFORE UPDATE OF user_id, received_at, text, url, image_path, image_mime,
                 client_ts, payload_hash, idempotency_key ON captures
BEGIN SELECT RAISE(ABORT, 'raw captures are immutable'); END;
CREATE TABLE processing_runs (
    id TEXT PRIMARY KEY,
    capture_id TEXT NOT NULL REFERENCES captures(id),
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL,
    normalized_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE jobs (
    capture_id TEXT PRIMARY KEY REFERENCES captures(id),
    status TEXT NOT NULL DEFAULT 'pending',
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    error TEXT
);
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES processing_runs(id),
    capture_id TEXT NOT NULL REFERENCES captures(id),
    sentence_id INTEGER NOT NULL,
    sentence TEXT NOT NULL,
    lemma TEXT NOT NULL,
    lemma_key TEXT NOT NULL,
    sense_key TEXT NOT NULL,
    surface TEXT NOT NULL,
    pos TEXT NOT NULL,
    gloss TEXT NOT NULL,
    cefr TEXT NOT NULL,
    domain TEXT NOT NULL,
    is_phrase INTEGER NOT NULL
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES processing_runs(id),
    item_id TEXT REFERENCES items(id),
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE cards (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    lemma_key TEXT NOT NULL,
    sense_key TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','added','updated','dropped','blocked')),
    anki_note_id INTEGER,
    added_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, lemma_key, sense_key)
);
CREATE TABLE encounters (
    card_id TEXT NOT NULL REFERENCES cards(id),
    capture_id TEXT NOT NULL REFERENCES captures(id),
    sentence TEXT NOT NULL,
    PRIMARY KEY(card_id, capture_id, sentence)
);
CREATE TABLE known_index (
    user_id TEXT NOT NULL REFERENCES users(id),
    lemma_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('known','learning','queued','suspended')),
    deck TEXT NOT NULL,
    note_id INTEGER NOT NULL,
    rank INTEGER,
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY(user_id, lemma_key, note_id)
);
CREATE TABLE promotions (
    user_id TEXT NOT NULL REFERENCES users(id),
    note_id INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    PRIMARY KEY(user_id, note_id)
);
CREATE TABLE write_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    backup_path TEXT,
    added_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    run_id TEXT REFERENCES processing_runs(id),
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX captures_user_received ON captures(user_id, received_at);
CREATE INDEX jobs_claim ON jobs(status, next_attempt_at, lease_until);
CREATE INDEX items_lemma ON items(lemma_key);
CREATE INDEX known_lookup ON known_index(user_id, lemma_key);
CREATE INDEX decisions_run ON decisions(run_id, item_id);
