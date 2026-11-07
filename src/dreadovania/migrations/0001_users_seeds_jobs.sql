-- SPDX-License-Identifier: GPL-3.0-or-later
-- Phase 1, PR 1: the tables the job runner needs (ADR 0011).
-- Sign-in tables arrive in a later migration; migrations only move forward.

CREATE TABLE users (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE seeds (
    id                  TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    status              TEXT NOT NULL
                        CHECK (status IN ('pending', 'ready', 'failed', 'cancelled')),
    -- What the user asked for: a bundled preset, or a permalink.
    source              TEXT NOT NULL CHECK (source IN ('preset', 'permalink')),
    preset_key          TEXT,
    requested_permalink TEXT,
    seed_number         INTEGER,
    race                INTEGER NOT NULL DEFAULT 0 CHECK (race IN (0, 1)),
    -- Filled in when generation succeeds.
    permalink           TEXT,
    seed_hash           TEXT,
    randovania_version  TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    completed_at        TEXT,
    CHECK ((source = 'preset') = (preset_key IS NOT NULL)),
    CHECK ((source = 'permalink') = (requested_permalink IS NOT NULL))
) STRICT;

CREATE INDEX seeds_by_owner ON seeds (owner_id, created_at);

CREATE TABLE jobs (
    id           TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    seed_id      TEXT NOT NULL REFERENCES seeds (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('generate')),
    status       TEXT NOT NULL
                 CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    progress     TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
) STRICT;

CREATE INDEX jobs_by_status ON jobs (status, created_at);
CREATE INDEX jobs_by_owner ON jobs (owner_id, status);
