-- SPDX-License-Identifier: GPL-3.0-or-later
-- Phase 3, PR 1: saved Switches for sending seeds over FTP (ADR 0016).

CREATE TABLE switches (
    id                 TEXT PRIMARY KEY,
    owner_id           TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name               TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    host               TEXT NOT NULL CHECK (length(host) BETWEEN 1 AND 253),
    port               INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    username           TEXT,
    -- ftpd's password, if one is set. Never returned by the API or logged
    -- (ADR 0016: it protects a LAN-only service and travels in clear over FTP).
    password           TEXT,
    last_test_at       TEXT,
    last_test_ok       INTEGER CHECK (last_test_ok IN (0, 1)),
    last_test_message  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
) STRICT;

CREATE INDEX switches_by_owner ON switches (owner_id, created_at);
