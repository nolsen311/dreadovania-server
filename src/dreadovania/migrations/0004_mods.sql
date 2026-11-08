-- SPDX-License-Identifier: GPL-3.0-or-later
-- Phase 2, PR 2: patched mods (ADR 0013 §2-3).
--
-- Jobs gain a second kind, 'export'. SQLite can't change a CHECK constraint in
-- place, so the jobs table is rebuilt (nothing references it).

CREATE TABLE jobs_new (
    id           TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    seed_id      TEXT NOT NULL REFERENCES seeds (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('generate', 'export')),
    status       TEXT NOT NULL
                 CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    progress     TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
) STRICT;

INSERT INTO jobs_new SELECT id, owner_id, seed_id, kind, status, progress, error,
    created_at, started_at, finished_at FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
CREATE INDEX jobs_by_status ON jobs (status, created_at);
CREATE INDEX jobs_by_owner ON jobs (owner_id, status);
CREATE INDEX jobs_by_seed ON jobs (seed_id, kind, created_at);

-- At most one mod per seed: the files live in seeds/<owner>/<seed>/mod/.
CREATE TABLE mods (
    seed_id             TEXT PRIMARY KEY REFERENCES seeds (id) ON DELETE CASCADE,
    owner_id            TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    status              TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'failed', 'cancelled')),
    -- Filled in when patching succeeds.
    game_version        TEXT,
    file_count          INTEGER,
    size_bytes          INTEGER,
    randovania_version  TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    completed_at        TEXT
) STRICT;
