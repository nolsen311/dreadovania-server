-- SPDX-License-Identifier: GPL-3.0-or-later
-- Client certificates for secure remote access (ADR 0023).
--
-- Rows are kept after their device or account is gone, so a revoked
-- certificate stays on the revocation list until it would have expired anyway.
-- No private key is stored: each one is handed out once, in a .p12.

CREATE TABLE client_certificates (
    id            TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL,             -- users.id (no foreign key: see above)
    device_id     TEXT,                      -- devices.id for a Switch, NULL for a browser
    kind          TEXT NOT NULL CHECK (kind IN ('device', 'browser')),
    name          TEXT NOT NULL,
    issuer        TEXT NOT NULL CHECK (issuer IN ('builtin', 'cloudflare', 'external')),
    serial        TEXT NOT NULL,             -- hex
    sha256        TEXT NOT NULL UNIQUE,      -- of the DER certificate, hex
    not_before    TEXT NOT NULL,
    not_after     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    revoked_at    TEXT,
    replaced_by   TEXT                       -- the certificate that renewed this one
) STRICT;

CREATE INDEX client_certificates_by_owner ON client_certificates (owner_id, revoked_at);
CREATE INDEX client_certificates_by_device ON client_certificates (device_id);

-- Removing an account or a paired device revokes its certificates, whichever
-- code path removes it.
CREATE TRIGGER revoke_certificates_of_removed_user BEFORE DELETE ON users
BEGIN
    UPDATE client_certificates SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE owner_id = OLD.id AND revoked_at IS NULL;
END;

CREATE TRIGGER revoke_certificates_of_removed_device BEFORE DELETE ON devices
BEGIN
    UPDATE client_certificates SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE device_id = OLD.id AND revoked_at IS NULL;
END;
