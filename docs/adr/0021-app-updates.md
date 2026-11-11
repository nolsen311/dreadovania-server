# 0021. The server keeps the Switch app up to date

- Status: Proposed
- Date: 2026-11-11

## Context

The Switch app (`dreadovania-nx`) is released on GitHub as one `.nro`, with its
SHA-256 and a build-provenance attestation (Sigstore, signed by the app's
release workflow). Updating it means downloading it on a computer and copying it
to the SD card, so the app and the server can drift apart.

Homebrew can update itself in place: the running app is already in memory, so
its `.nro` on the SD card can be replaced while it runs, and libnx's
`envSetNextLoad` asks the Homebrew Menu to start the new one when the app exits.

The Switch already trusts its server (HTTPS with the server's certificate,
`dreadovania-nx` ADR 0006) and holds a token for it; the server can reach GitHub.

## Decision

1. **The server finds the latest app release.** It asks GitHub's API for the
   latest release of `nolsen311/dreadovania-nx` at most every 6 hours, when the
   app asks, and caches the answer. `DREADOVANIA_APP_UPDATES=off` turns this off
   for a server that shouldn't reach GitHub; the app then shows no updates.
2. **The server checks a release before offering it.** It downloads the `.nro`
   once into `data/app/<version>/`, checks its SHA-256 against the release's
   `.sha256`, and verifies its build-provenance attestation: the Sigstore bundle
   from GitHub's attestations API must be signed for that file's digest by
   `nolsen311/dreadovania-nx`'s `release.yml` on the release's tag. This is the
   check `gh attestation verify` does, so a tampered release or a substituted
   file is refused even though the checksum comes from the same release. It uses
   the `sigstore` Python library (a new dependency, for exactly this), with the
   Sigstore trust root built into the library (its offline mode), so the server
   contacts only GitHub; the trust root is updated whenever Dependabot bumps
   `sigstore`. Only a verified `.nro` is ever served; an unverified one is
   deleted and the reason logged.
   - Checked before writing this: the real `v0.4.0` release verifies (the
     signature covers the `.nro`'s exact SHA-256, signed by `release.yml` on tag
     `v0.4.0`), and asking for another tag is refused.
3. **Two routes for the app, open to device tokens:**
   - `GET /api/v1/app/latest`: `{version, size, sha256, minimum_version}`, or
     `{available: false, reason}` when there is no verified release (offline,
     turned off, or verification failed).
   - `GET /api/v1/app/dreadovania.nro`: the verified file.
4. **A minimum app version.** The server states the oldest app version it works
   with (`minimum_version`, a constant raised when the API changes in a way older
   apps can't handle). An older app says it must update before it does anything
   else; a newer server never silently breaks an old app.
5. **The app updates only when asked.** When it connects, the app compares
   versions. If a newer one is verified, the Library shows
   "Update to X available" with a button; nothing downloads without the button,
   and never during an install. Updating:
   1. downloads to a temporary file next to its own `.nro` (its path is the one
      it was started from), checking size and SHA-256 as it arrives;
   2. renames it over its own `.nro` only when complete, so a failed update
      leaves the old app untouched;
   3. sets `envSetNextLoad` to its own path and exits, so the new version starts.
   If the app was started some other way, where the next load can't be set, it
   says to restart it by hand.
6. **Rollback is by hand.** Older releases stay on GitHub; the app offers only
   the latest.

## Pull requests

1. This ADR.
2. Server: the release check, verification, cache, the two routes, the minimum
   version and the setting; tests use a local fake of GitHub's API and a signed
   test bundle where possible, and the real verification is exercised once
   against a published release.
3. App: the update check, the Library prompt, the download, the in-place swap
   and the restart, with host tests for the version comparison and the swap,
   and a hardware checklist.

## Consequences

- Updating the app becomes: open it, press the update button.
- The server now contacts `api.github.com` and GitHub's release downloads, every
  6 hours at most while the app is in use, unless turned off. It doesn't need
  to reach Sigstore's servers.
- If Sigstore rotates its trust root before `sigstore` is updated here,
  verification fails and no update is offered (it never falls back to
  unverified); the next `sigstore` bump fixes it.
- A new server dependency (`sigstore`) and its own dependencies.
- The app trusts the server to have verified the release; a compromised server
  could serve a bad app. It could already send the Switch a bad seed, so this
  doesn't widen what a compromised server can do.
