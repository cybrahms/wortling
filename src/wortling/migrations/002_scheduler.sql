CREATE TABLE scheduler_state (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    next_sync_at TEXT NOT NULL,
    last_report_day TEXT,
    last_sync_at TEXT
);
CREATE TABLE backup_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);
