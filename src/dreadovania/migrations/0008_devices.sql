-- SPDX-License-Identifier: GPL-3.0-or-later
-- Paired devices for the Switch app (ADR 0004, ADR 0017). Tokens and pairing
-- codes are stored only as SHA-256 hashes.

CREATE TABLE devices (
    id            TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    last_ip       TEXT
) STRICT;

CREATE INDEX devices_by_owner ON devices (owner_id);

-- At most one live code per user; a new one replaces it.
CREATE TABLE pairing_codes (
    owner_id    TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    code_hash   TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL
) STRICT;
