-- SPDX-License-Identifier: GPL-3.0-or-later
-- Randovania's cosmetic options (ADR 0025): each account's own, and the ones each
-- patched seed was patched with. A mod patched before this has none recorded,
-- which means Randovania's defaults.

CREATE TABLE cosmetics (
    owner_id    TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    options     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

ALTER TABLE mods ADD COLUMN cosmetics TEXT;
