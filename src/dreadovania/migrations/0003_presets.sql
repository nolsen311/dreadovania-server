-- SPDX-License-Identifier: GPL-3.0-or-later
-- Phase 1, PR 3: user-imported presets (ADR 0011 §3).
-- A seed made from an imported preset stores preset_key = 'user:<preset id>'.

CREATE TABLE presets (
    id           TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,  -- the .rdvpreset JSON, at most 1 MB
    created_at   TEXT NOT NULL
) STRICT;

CREATE INDEX presets_by_owner ON presets (owner_id, created_at);

-- The preset's display name at the time the seed was made, so history still
-- reads well after a preset is renamed or deleted.
ALTER TABLE seeds ADD COLUMN preset_name TEXT;
