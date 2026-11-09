# 0017. Pairing the Switch app: codes, device tokens and what they may do

- Status: Accepted
- Date: 2026-11-09

## Context

ADR 0004 decided that the Switch app signs in with a short pairing code
exchanged for a long-lived device token, so nobody types a password on a
Switch. Phase 4 (`dreadovania-nx` ADR 0004) needs it now. This ADR records
the details.

## Decision

- **Pairing code:** from the Account page, signed in. 8 characters from an
  alphabet without look-alikes (no 0/O, 1/I), shown as `ABCD-EFGH`; typed with
  or without the dash, in any case. It works once, for 5 minutes, and a new
  code replaces the previous one. Exchanging a code is rate-limited like
  sign-in (10 failures per client, 50 overall, per 15 minutes).
- **Device token:** `dvt_` plus 32 random bytes, returned once when pairing.
  Only its SHA-256 hash is stored. The app sends it as
  `Authorization: Bearer …`, so no Origin check is needed: browsers never send
  it on their own.
- **What a token may do** is an explicit list of routes
  (`devices.DEVICE_ROUTES`):
  - list, read and generate seeds, and read a seed's spoiler log;
  - patch a seed and follow the job;
  - read presets;
  - read and remove its own pairing (`/devices/me`).

  Everything else returns `403`: account settings, passkeys, sessions,
  Switches, game files, presets import or deletion, purging, other devices
  and pairing codes. A test checks that every listed route exists, so a typo
  can't silently lock the app out.
- **Records:** each device has a name (the console's), when it was paired and
  last used, and the address it last connected from. The Account page lists
  them and removes one at once. At most 20 per user.
- **Later:** downloading a patched seed's files is added with the per-file API
  (`dreadovania-nx` ADR 0004), and joins the list then.

## Consequences

- A lost or sold Switch is handled by removing it on the Account page; the
  password and passkeys are unaffected.
- A token can create seeds and patch them (using CPU and disk), but can't
  reach the user's game-file upload, saved FTP passwords or account.
