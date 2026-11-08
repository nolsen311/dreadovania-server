# 0016. Phase 3 plan: sending a patched seed to the Switch over FTP

- Status: Accepted
- Date: 2026-11-08
- Builds on ADR 0006 (Switch delivery) and ADR 0013 (patching). Brought forward
  before TLS and passkeys at the maintainer's request: copying a mod over MTP is
  very slow, because a mod is many small files.

## Context

A patched seed (ADR 0013) is a folder laid out like the SD card's `atmosphere/`.
Patching uses Randovania's exlaunch mode, so the mod holds only the assets the
seed changes plus a small loader, typically tens of MB. Two facts follow:

- **Installing over a previous seed leaves its extra files behind**, mixing two
  seeds. The previous seed's files have to be removed first.
- **Other mods can coexist** (for example cosmetic mods), as long as they don't
  replace the same files. Randovania's own Dread cosmetics (missile colours, HUD,
  room names, volumes, alternative beam looks, separate save slots per seed) don't
  include Samus's suit colours, so those come from other mods.

The maintainer runs mtheall's `ftpd` on the Switch (default port 5000).

## Decision

### 1. Saved Switches

- **Add Switch:** a name, an address, a port (default 5000) and an optional
  username and password, stored per owner. The password is never shown again,
  never logged, and only replaced or cleared.
- **LAN only:** the address must be, or resolve only to, a private, loopback or
  link-local address. The server never connects to public addresses on a user's
  behalf, and FTP is unencrypted.
- **Test connection:** log in, check that `/atmosphere` exists, and report
  plainly what failed (unreachable, refused, wrong login, not an Atmosphere SD card).
- In Phase 4, pairing the Switch app upgrades this record rather than adding one.

### 2. Send to Switch: install the seed now

- A patched seed gets a **Send to Switch** action. It runs as a `push` job in the
  existing single-worker queue, with live progress (files and bytes).
- **Install directly** (maintainer's choice): the seed's files go to
  `atmosphere/contents/010093801237c000/` and `atmosphere/exefs_patches/…`, ready
  to play. Keeping several seeds in a SimpleModManager library is left to
  Phase 4, which swaps seeds on the console.
- The guided flow from the plan: "Open ftpd on your Switch" → automatic
  connection check → send, with progress → "Close ftpd and launch Dread with a
  new save".
- Plain FTP with Python's `ftplib` (standard library, passive mode), with
  timeouts. No new runtime dependency.

### 3. Clean installs without touching other mods

- **Manifest:** every send records exactly which files it installed, on the
  server (per Switch) and on the SD card (`/config/dreadovania/installed.json`).
  The manifest of planned files is written **before** uploading, so a send that
  is interrupted can still be cleaned up.
- **Cleanup:** the next send first removes only the files in the previous
  manifest (and directories that become empty), so other mods are left alone.
- **Conflicts:** if the new seed would overwrite a file that Dreadovania didn't
  install (likely another mod's), the send stops and lists those files. Sending
  again with **Overwrite** replaces them; they are then Dreadovania's and are
  removed by the next cleanup.
- **First send to a Switch with existing files** in Dread's mod folder: they are
  treated as another mod's and left alone, except where the seed conflicts with
  them (above). A **Clear Dread's mod folder** option is available for users who
  keep only randomizer seeds there.

### 4. Pull requests, in order

1. **Saved Switches:** the table, the API, the "Switches" page, LAN-only
   validation and the connection test.
2. **Send to Switch:** the `push` job, manifests, cleanup, conflict detection, the
   guided flow and progress in the UI.

### Later (not part of Phase 3)

- **Extra mods:** upload a mod (for example a suit-colour mod) to the server once,
  and have it layered onto every send, with the same conflict report.
- **Randovania's cosmetic options**, including separate save slots per seed: the
  patcher data currently uses Randovania's defaults.

## Consequences

- A send takes as long as FTP needs for many small files over Wi-Fi, but needs
  no PC and no MTP.
- Tests run against a local FTP server, not a Switch. The real path is verified by
  the maintainer on their own Switch before each Phase 3 PR merges, with a short
  checklist in the PR.
- The FTP password is stored in the server's database in plain text: it protects
  a LAN-only, usually anonymous service and must be sent in clear over FTP anyway.
  It is never returned by the API or logged.
- Manual installs from the zip must remove the previous seed first; the docs and
  the seed page now say so.
