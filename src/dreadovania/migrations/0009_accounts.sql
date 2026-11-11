-- SPDX-License-Identifier: GPL-3.0-or-later
-- Accounts for friends on the same server, by invitation (ADR 0020).

-- The account created at setup is the admin; invited accounts are members.
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'
    CHECK (role IN ('admin', 'member'));
UPDATE users SET role = 'admin' WHERE id = (
    SELECT id FROM users WHERE username IS NOT NULL ORDER BY created_at, rowid LIMIT 1
);

-- One-time invite links. The token is stored only as a SHA-256 hash; an invite
-- is deleted when it is used or revoked.
CREATE TABLE invites (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    created_by  TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
) STRICT;
