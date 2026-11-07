# 0006. Switch delivery: zip, FTP push, SimpleModManager-compatible layout

- Status: Accepted
- Date: 2026-11-07

## Context

The user runs mtheall's `ftpd` as an app they launch, and manages mods with
SimpleModManager. The long-term goal is an all-in-one Switch app (ADR 0007), but
seeds should be easy to swap before it exists.

## Decision

- **Zip download:** always available, as the fallback.
- **FTP push:**
  - Goes to a **saved Switch** (name, IP, port — default 5000 — and an optional login),
    with a "Test connection" button.
  - The flow is: "open ftpd" → automatic connection check → send with progress →
    "close ftpd and launch Dread".
- **Library layout:** every seed is written as its own mod in **SimpleModManager's
  library layout**, so it can be swapped from SimpleModManager today and from
  `dreadovania-nx` later. The exact layout is to be confirmed.
- **Pairing:** when the Switch app is paired, the saved Switch record is upgraded rather than duplicated.
- **Security:** FTP is unencrypted, so it is documented as LAN only. The ftpd password
  is never logged or shown back in the UI.

## Consequences

- No Switch-side code is needed before Phase 4.
- We depend on SimpleModManager's folder layout staying stable. If it changes, only
  the layout writer needs updating.
- Save data per seed isn't handled by the server. Until the Switch app does it, the
  docs say "one save slot per seed".
