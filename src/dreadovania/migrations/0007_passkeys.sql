-- SPDX-License-Identifier: GPL-3.0-or-later
-- Passkeys (WebAuthn), ADR 0004. Only public keys are stored: a passkey's private
-- key never leaves the user's device or password manager.

CREATE TABLE passkeys (
    id              TEXT PRIMARY KEY,  -- the credential ID, base64url
    user_id         TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    public_key      BLOB NOT NULL,     -- COSE key
    sign_count      INTEGER NOT NULL,
    rp_id           TEXT NOT NULL,     -- the hostname it was created for
    created_at      TEXT NOT NULL,
    last_used_at    TEXT
) STRICT;

CREATE INDEX passkeys_by_user ON passkeys (user_id);

-- One-time challenges, each valid for a few minutes. A sign-in challenge has no
-- user yet: the passkey itself says who is signing in.
CREATE TABLE webauthn_challenges (
    id          TEXT PRIMARY KEY,
    purpose     TEXT NOT NULL CHECK (purpose IN ('register', 'sign_in')),
    user_id     TEXT REFERENCES users (id) ON DELETE CASCADE,
    challenge   BLOB NOT NULL,
    expires_at  TEXT NOT NULL
) STRICT;
