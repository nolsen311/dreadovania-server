# 0001. Support Metroid Dread only

- Status: Accepted
- Date: 2026-11-07

## Context

Randovania supports many games. Only Metroid Dread runs natively on the Switch,
which is the target device. Each extra game adds native patchers, image size and
work to verify arm64 support.

## Decision

Support **Metroid Dread only**, for both generating and patching.

## Consequences

- The image ships only the Dread toolchain, so it is smaller and arm64 is more realistic.
- The user-supplied input is the Dread RomFS (and ExeFS if needed).
- Output is a LayeredFS mod for title ID `010093801237C000`.
- Adding other games later means a new ADR.
