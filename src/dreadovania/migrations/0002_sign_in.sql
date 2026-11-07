-- SPDX-License-Identifier: GPL-3.0-or-later
-- Phase 1, PR 2: sign-in (ADR 0004, ADR 0011 §4).
-- Secrets (session and setup tokens) are only ever stored as SHA-256 hashes.

-- SQLite can't add a UNIQUE column with ALTER TABLE, so uniqueness is an index.
ALTER TABLE users ADD COLUMN username TEXT;
CREATE UNIQUE INDEX users_by_username ON users (username COLLATE NOCASE);

CREATE TABLE credentials (
    user_id        TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    password_hash  TEXT NOT NULL,  -- Argon2id, in the PHC string format
    updated_at     TEXT NOT NULL
) STRICT;

CREATE TABLE sessions (
    token_hash    TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL
) STRICT;

CREATE INDEX sessions_by_user ON sessions (user_id);

CREATE TABLE setup_tokens (
    token_hash  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
) STRICT;

-- Instance-wide settings chosen in the setup wizard, e.g. the public URL.
CREATE TABLE instance_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
) STRICT;
