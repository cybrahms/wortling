CREATE TABLE capture_sources (
    capture_id TEXT PRIMARY KEY REFERENCES captures(id),
    final_url TEXT NOT NULL,
    content_type TEXT NOT NULL,
    body BLOB NOT NULL,
    source_text TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TRIGGER capture_sources_immutable BEFORE UPDATE ON capture_sources
BEGIN SELECT RAISE(ABORT, 'fetched capture sources are immutable'); END;
