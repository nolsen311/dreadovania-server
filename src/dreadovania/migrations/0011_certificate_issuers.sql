-- SPDX-License-Identifier: GPL-3.0-or-later
-- Certificates from an outside issuer (ADR 0023): Cloudflare's own ID for each
-- one, and when its revocation reached that issuer. Removing a device or an
-- account revokes in the database (by trigger); the server then passes it on.

ALTER TABLE client_certificates ADD COLUMN issuer_id TEXT;
ALTER TABLE client_certificates ADD COLUMN issuer_revoked_at TEXT;

CREATE INDEX client_certificates_to_revoke_at_issuer
    ON client_certificates (issuer, revoked_at, issuer_revoked_at);
