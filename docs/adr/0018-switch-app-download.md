# 0018. The Switch app downloads a patched seed as a manifest and one tar

- Status: Accepted
- Date: 2026-11-10
- Amends: [0017](0017-device-pairing.md) ("Later": the per-file API) and
  `dreadovania-nx` ADR 0004, decision 3 (per-file downloads)

## Context

The plan was for the app to fetch a patched seed file by file. A seed is about
25 MB but several hundred files (a real send listed 394), and on the Switch each
new HTTPS connection costs a handshake through the console's TLS service. Per
file would turn seconds into minutes, for little gain on a LAN.

What the app needs is all-or-nothing: either the whole seed arrives and checks
out, or nothing on the card changes.

## Decision

- `GET /seeds/{id}/mod/manifest`: every file with its SD path
  (`atmosphere/…`), size and SHA-256, the totals, and the exact length of the tar.
- `GET /seeds/{id}/mod.tar`: one **uncompressed ustar** stream of the same files
  in the same order, with `Content-Length`. Plain ustar only (names up to 255
  characters through the prefix field), so the app's reader stays small; a name
  that doesn't fit is an error, not a PAX extension.
- Both are in the device token's allowed routes. The zip download stays for
  browsers and manual installs, and isn't open to devices.
- The app downloads into a staging folder, hashing each file as it arrives and
  checking it against the manifest. Only when every file matches does it install,
  with the same manifest-first steps as Send to Switch (ADR 0016). Any error
  discards the staging folder. A dropped connection means downloading the
  ~25 MB again, which is quicker than the code to resume.

## Consequences

- Two requests per install, and every byte is verified before the card changes.
- The SHA-256s are computed per request (about 25 MB of hashing); cache them if
  that ever shows up.
