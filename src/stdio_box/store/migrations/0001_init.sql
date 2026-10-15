CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE profiles (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    name TEXT NOT NULL,
    variant TEXT NOT NULL,
    description TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('awaiting_secrets', 'ready', 'check_failed', 'needs_reentry')
    ),
    public_fields TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    rotated_at TEXT,
    rotated_by TEXT,
    last_check_at TEXT,
    last_check_ok INTEGER,
    last_check_identity TEXT
);

CREATE TABLE profile_secrets (
    profile_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    key_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, version),
    FOREIGN KEY (profile_id) REFERENCES profiles (id)
);

CREATE TABLE bindings (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    position INTEGER,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY (provider, subject),
    UNIQUE (provider, position),
    CHECK ((subject LIKE 'role:%') = (position IS NOT NULL)),
    FOREIGN KEY (profile_id) REFERENCES profiles (id)
);

CREATE TABLE form_links (
    token_hash TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('create', 'rotate')),
    pending_public_fields TEXT,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles (id)
);

CREATE INDEX form_links_profile_id ON form_links (profile_id);

CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    sub TEXT,
    jti TEXT,
    tool TEXT,
    profile_id TEXT,
    tier TEXT,
    outcome TEXT NOT NULL,
    code TEXT,
    duration_ms INTEGER,
    detail TEXT
);

CREATE INDEX audit_at ON audit (at);
CREATE INDEX audit_profile_id ON audit (profile_id);

CREATE TABLE status (
    body TEXT NOT NULL
);
