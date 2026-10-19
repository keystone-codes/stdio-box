-- Raw tools/list snapshots taken by creds_snapshot_tools. The newest five are kept.
CREATE TABLE tool_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    profile TEXT NOT NULL,
    variant TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL
);
