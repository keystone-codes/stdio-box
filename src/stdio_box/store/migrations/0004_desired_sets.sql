-- Versions of the profile set written by creds_desired_set. Rows are never changed.
CREATE TABLE desired_sets (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    body TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    restored_from INTEGER
);
