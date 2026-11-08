-- SPDX-License-Identifier: GPL-3.0-or-later
-- Phase 3, PR 2: sending a patched seed to a Switch (ADR 0016).
--
-- Jobs gain a third kind, 'push', and a JSON params column (which Switch, and
-- the send's options). SQLite can't change a CHECK constraint in place, so the
-- jobs table is rebuilt (nothing references it).

CREATE TABLE jobs_new (
    id           TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    seed_id      TEXT NOT NULL REFERENCES seeds (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('generate', 'export', 'push')),
    status       TEXT NOT NULL
                 CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    params       TEXT,
    progress     TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
) STRICT;

INSERT INTO jobs_new (id, owner_id, seed_id, kind, status, progress, error, created_at,
                      started_at, finished_at)
    SELECT id, owner_id, seed_id, kind, status, progress, error, created_at, started_at,
           finished_at FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX jobs_by_status ON jobs (status, created_at);
CREATE INDEX jobs_by_owner ON jobs (owner_id, status);
CREATE INDEX jobs_by_seed ON jobs (seed_id, kind, created_at);

-- What Dreadovania last installed on each Switch: the files it owns there, so
-- the next send removes exactly those and leaves other mods alone. Written with
-- the planned files *before* uploading, so an interrupted send can be cleaned.
CREATE TABLE switch_installs (
    switch_id   TEXT PRIMARY KEY REFERENCES switches (id) ON DELETE CASCADE,
    owner_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    seed_id     TEXT,  -- not a foreign key: the seed may be deleted, the files remain
    files       TEXT NOT NULL,  -- JSON list of SD card paths, like /atmosphere/contents/...
    state       TEXT NOT NULL CHECK (state IN ('installing', 'installed')),
    updated_at  TEXT NOT NULL
) STRICT;
