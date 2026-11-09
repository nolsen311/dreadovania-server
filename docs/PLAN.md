# Dreadovania: project plan

Status: accepted (2026-11-07). Decisions are recorded as ADRs in [`docs/adr/`](adr/).
Items marked **(verify)** are assumptions to confirm during the Phase 0 spike.

## Goals

- A Docker container that wraps Randovania to generate and patch **Metroid Dread** seeds.
- A web UI that is pleasant to use from a phone.
- Delivery to a modded Switch (Atmosphère): zip download and FTP push first, then a
  companion homebrew app ([`dreadovania-nx`](https://github.com/nolsen311/dreadovania-nx)).
- A CI/CD pipeline built so that every published build is verifiably safe.
- **No game content in the repo or images, ever.** Users supply their own.

## Decisions at a glance

| Topic | Decision | ADR |
|---|---|---|
| Games | Metroid Dread only, for generating and patching | [0001](adr/0001-dread-only.md) |
| Randovania integration | CLI only (Phase 0 found no adapter needed); Randovania and the Dread patcher pinned together | [0002](adr/0002-randovania-integration.md), [0010](adr/0010-phase0-randovania-findings.md) |
| Platforms | x86-64 first; arm64-ready with no rework; weekly arm64 test build; arm64 image blocked on `py-tegra-swizzle` wheels | [0003](adr/0003-x86-first-arm64-ready.md), [0010](adr/0010-phase0-randovania-findings.md) |
| Users | Single user now; every record has an `owner_id` so accounts can be added later | [0004](adr/0004-auth.md) |
| Sign-in | Setup wizard; passkeys and autofill-friendly password; device tokens for the NRO | [0004](adr/0004-auth.md) |
| TLS | Bring-your-own cert files with hot reload; Let's Encrypt via DNS-01 recipes; no ACME in the app | [0005](adr/0005-tls-lets-encrypt.md) |
| Switch delivery | Zip download and FTP push to `ftpd`; SimpleModManager-compatible library layout | [0006](adr/0006-switch-delivery.md) |
| Switch app | Separate repo `dreadovania-nx`, using this server's OpenAPI spec as its contract | [0007](adr/0007-separate-nro-repo.md) |
| Web UI | Server-rendered (Jinja + htmx), no Node.js toolchain | [0011](adr/0011-phase1-plan.md) |
| Dependency updates | Dependabot (pip/uv, Docker digests, Actions SHAs) | [0008](adr/0008-ci-supply-chain.md) |
| Authorship and sign-off | Claude authors; maintainer certifies each merge with a DCO sign-off, by hand or with a `/signoff` comment | [0009](adr/0009-claude-authorship-and-dco.md), [0012](adr/0012-signoff-by-comment.md) |
| Releases | The maintainer releases a merged version bump with a `/release` comment; the maintainer's release app creates the tag; Claude never tags | [0014](adr/0014-release-by-comment.md), [0015](adr/0015-release-app.md) |

## Phases

| Phase | Scope | Needs game files? |
|---|---|---|
| **0. Spike** | Confirm Randovania's CLI generates a Dread seed headless in a Debian-slim container. Confirm how Dread *export* can be triggered without the GUI. Check Qt headless needs. | No |
| **1. Generate** ([ADR 0011](adr/0011-phase1-plan.md)) | Web UI, setup wizard and authentication, TLS support, the full CI/CD pipeline. | No |
| **2. Patch** ([ADR 0013](adr/0013-phase2-patching.md)) | Dread RomFS goes in, a mod comes out, and you can download it as a zip. | Yes (user-supplied) |
| **3. Send to Switch** ([ADR 0016](adr/0016-phase3-send-to-switch.md)) | FTP push to `ftpd`, installed directly with clean replacement of the previous seed (other mods kept), with a saved Switch. | Yes |
| **4. NRO** | `dreadovania-nx`: pair, generate, download and swap seeds on the Switch. | Yes |

## 1. Generating vs. patching

Randovania works in two stages:

- **Generate:** a preset goes in and a `.rdvgame` layout comes out. No game files are needed.
- **Export/patch:** the layout plus the user's own game files go in, and a Dread mod comes out.

Splitting the phases this way lets Phase 1 ship with zero copyrighted input.

## 2. Bringing Randovania into the container

- Use `python -m randovania ...` as a subprocess wherever the CLI covers the task.
- If Dread export isn't available from the CLI **(verify)**, call Randovania's export
  code through **one adapter module** so upstream API changes stay contained.
- Pin Randovania to an exact version. `.rdvgame` files and permalinks are version-bound,
  so show the version in the UI and in every seed's details.
- Ship only the Dread toolchain (`open-dread-rando`, `mercury-engine-data-structures`).
- Phase 0 (ADR 0010) confirmed: no Qt is needed (PySide6 is the `gui` extra), and only
  `open-dread-rando` is installed rather than the `exporters` extra, which pulls in every
  game's patcher. Generation and export both run headless through the CLI.
- License: GPL-3.0-or-later, compatible with Randovania. The image carries OCI source labels and a
  `SOURCE.md` pointing to the exact source of every bundled GPL component.
- Upstream: draft a friendly note to the Randovania developers before announcing publicly.

## 3. User-supplied content and the legal line

- **What the user provides:** the RomFS (and ExeFS, if needed **(verify)**) extracted from
  their own Dread dump.
- **How it gets in:** a mounted volume (`/data/input`) is the main route, because these
  files are several GB and best copied over SMB or SCP. A resumable browser upload (tus)
  is a later, optional addition.
- **Validation:** hashes are checked against the Dread versions Randovania supports
  **(verify which)**, with clear "wrong version" errors.
- **Never in the repo or image:** game assets, keys (`prod.keys`, title keys), or links to them.
  A CI guard rejects `.nsp`, `.xci`, `.nca`, `.nsz`, `.xcz`, `romfs/`, `exefs/`, `*.keys`
  and large binaries.
- **Logs:** never log file contents or paths that reveal a user's library.
- **Docs:** point to official homebrew dumping tools only in general terms; output is for personal use.
- **Branding:** "Dreadovania"; no "Metroid" in names; original icon art only; no Nintendo artwork.

## 4. Mobile web UI

- **Backend:** FastAPI. Its generated OpenAPI spec is the official contract for `dreadovania-nx`.
- **Frontend:** server-rendered pages (Jinja templates) with htmx, vendored and
  hash-checked, served by FastAPI. No Node.js toolchain anywhere; installable as a
  PWA ([ADR 0011](adr/0011-phase1-plan.md), superseding "static PWA (Svelte or Preact)").
- **Jobs:** generation and patching run in a single-worker queue, with live progress over SSE.
- **Screens:**
  - First-run setup wizard
  - Presets (import and export `.rdvpreset`)
  - Permalinks
  - Seed history, with purge of all seeds or all older than *N* days
  - Spoiler log behind an explicit reveal step
  - Add Switch, with a connection test
  - Send to Switch
  - Paired devices (list and revoke)
- **Out of scope for v1:** the full preset editor and multiworld.

## 5. The Switch side

- **Install location:** Dread mods live under `atmosphere/contents/010093801237C000/`.
- **Library layout:** each seed is written as its own mod in SimpleModManager's library
  layout (roughly `sd:/mods/<Game>/<Mod name>/...`) **(verify exact layout)**, so seeds
  can be swapped from SimpleModManager before our NRO exists.
- **FTP push (Phase 3):** to mtheall's `ftpd` (default port 5000). The guided flow is:
  1. "Open ftpd on your Switch."
  2. Automatic connection check.
  3. Send, with progress shown.
  4. "Close ftpd and launch Dread."
- **Saved Switch:** a one-time "Add Switch" form (name, IP, port, optional login). When
  the NRO is paired later, this record is upgraded rather than duplicated. The docs
  recommend a DHCP reservation for the Switch.
- **FTP is unencrypted:** LAN only, and documented as such. The NRO replaces it with HTTPS.
- **Save data:** each seed needs its own save. Until the NRO handles swapping saves,
  the docs say "one save slot per seed" **(verify Dread save-slot behaviour)**.

## 6. Security of the running container

- **Container settings:**
  - Runs as a non-root user
  - Read-only root filesystem
  - `cap_drop: [ALL]`
  - `no-new-privileges`
  - Memory and CPU limits
- **Sign-in:**
  - The one-time setup link is printed to the container logs on first run.
  - The user chooses a passkey and/or a password (with `autocomplete` attributes so
    password managers work).
  - A 90-day session cookie (`Secure`, `HttpOnly`, `SameSite=Lax`) renews as you use it.
  - Logins are rate-limited, and forms are protected against CSRF.
- **Device tokens:** pairing code → long-lived device token. Only a hash is stored. Tokens
  are narrowly scoped (list, download, generate from preset), revocable, and tied to `owner_id`. Details: [ADR 0017](adr/0017-device-pairing.md).
- **TLS:** see [ADR 0005](adr/0005-tls-lets-encrypt.md). The app needs a configured public URL
  and a trusted-proxy allowlist, and redirects requests by IP address to the configured hostname.
  Setup and recipes: [docs/https.md](https.md).
- **Uploads:** treated as hostile. Size limits, safe archive extraction (no zip-slip),
  and nothing from an upload is ever executed.
- **Secrets:** the ftpd password is stored server-side, never logged and never shown back in the UI.

## 7. CI/CD supply chain

**Repository settings**
- A ruleset on `main` ([`.github/rulesets/protect-main.json`](../.github/rulesets/protect-main.json)).
- Secret scanning with push protection.
- Private vulnerability reporting, a `SECURITY.md` and a `CODEOWNERS` file.

**GitHub Actions**
- Every action pinned to a full commit SHA.
- `permissions: {}` at the top level, with permissions granted per job.
- No `pull_request_target` with a checkout of PR code.
- Workflows checked with `zizmor` and `actionlint`.
- `step-security/harden-runner` to watch network egress.

**Dependencies**
- `uv` lockfile with hashes.
- `pip-audit` / OSV-Scanner.
- CodeQL for Python and JavaScript.
- Dependabot updates for uv, Docker and Actions.

**Image**
- Multi-stage build on Debian slim, pinned by digest.
- Built with `docker buildx`; the target platforms are one variable.
- Scanned with Grype (from Anchore, which also makes the Syft SBOM tool) and fails on fixable high or
  critical findings. Releases push by digest, scan every platform, and only then apply tags.
- The content guard (section 3) runs on every PR.

**Releases**
- Built from tags only, on GitHub-hosted runners, and pushed to GHCR.
- Syft SBOM.
- SLSA build provenance (`actions/attest-build-provenance`).
- Signed with cosign keyless signing.
- The docs show how to check a release with `cosign verify` or `gh attestation verify`.

**Monitoring**
- An OpenSSF Scorecard workflow and its badge.
- A weekly arm64 test build that doesn't block anything and opens or updates an issue when it fails.

## Open items

Resolved by Phase 0 (ADR 0010):

- ~~Whether Randovania's CLI covers Dread export~~: it does; no adapter module needed.
- ~~Qt/PySide6 headless requirements~~: none.
- ~~Whether the Dread toolchain resolves for arm64~~: the lock resolves, but
  `py-tegra-swizzle` has no aarch64 wheel, so the arm64 image is blocked (options in ADR 0010).

Resolved since:

- ~~Supported Dread versions; whether ExeFS is needed~~: Randovania exports from
  1.0.0 and 2.1.0, identified by `system/files.toc`, and needs only the RomFS
  (ADR 0013); confirmed on the maintainer's own game files with v0.1.1.
- ~~SimpleModManager's library layout~~: `/mods/<game>/<mod>/<tree under atmosphere/>`
  (its README); it is GPLv3. Phase 3 installs directly instead (ADR 0016); its
  code is a reference for Phase 4.

Still open:

1. How cleanly SimpleModManager's core logic separates from its UI (Phase 4).
2. How Dread save slots behave across different seeds. Randovania has a
   "separate save slots" cosmetic option for Dread, to expose with the other
   cosmetics (ADR 0016, "Later").
