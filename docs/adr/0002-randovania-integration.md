# 0002. Integrate Randovania via its CLI, with one adapter module

- Status: Accepted
- Date: 2026-11-07

## Context

Randovania is primarily a PySide6 desktop app. Its internal Python API changes
between releases. Its CLI is a more stable surface but may not cover every task,
in particular exporting (patching) Dread.

## Decision

- Run `python -m randovania ...` as a subprocess wherever the CLI is enough.
- Where it isn't, call Randovania's code through a **single adapter module**, so
  version-specific details live in one place.
- Pin Randovania to an exact version, and record that version on every seed.

## Consequences

- An upgrade means bumping the pin, then testing the adapter and the CLI calls.
- `.rdvgame` files and permalinks are tied to a version, which the UI shows.
- Phase 0 has to establish what the CLI can do and what Qt needs when headless.
