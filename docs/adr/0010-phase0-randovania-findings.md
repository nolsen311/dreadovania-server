# 0010. Phase 0: Randovania runs headless through its CLI

- Status: Accepted
- Date: 2026-11-07
- Refines [ADR 0002](0002-randovania-integration.md) and records an arm64
  blocker for [ADR 0003](0003-x86-first-arm64-ready.md).

## Context

Phase 0 had to establish, before any feature work, whether Randovania can
generate and export Metroid Dread seeds inside our hardened, GUI-less
container, and at what cost. Everything below was measured with
Randovania 12.0.0 and open-dread-rando 2.21.0 on linux/amd64.

## Findings

| Question | Result |
|---|---|
| Does the core install need Qt? | **No.** PySide6 is only in Randovania's `gui` extra. Nothing Qt-related is installed. |
| Can we install only the Dread patcher? | **Yes.** Depend on `randovania` plus `open-dread-rando` at the exact version Randovania's `exporters` extra pins, instead of the extra (which installs every game's patcher). |
| Headless generation? | **Yes.** `layout generate-from-presets --file <preset>` produced a valid 78 KB `.rdvgame` from the bundled Dread starter preset in about 17 s on one core (about 19 s in the hardened container). Peak memory: about 150 MB. |
| Deterministic? | **Yes.** `layout generate-from-permalink` with the resulting permalink reproduced the same hash. |
| Headless export? | **Yes, via the CLI.** `layout patcher-data` writes the patcher JSON; `layout export-game … dread --target-platform atmosphere --input-path <RomFS> --output-path <out>` runs the Dread patcher. Without game files it stops exactly where it reads the user's RomFS (`system/files.toc`). |
| Works under our hardening? | **Yes.** Non-root UID 10001, read-only root filesystem, no capabilities, 2 GB memory limit: generation and export behave the same; only the output directory is written. |
| Telemetry? | **None from the paths we use.** Randovania depends on `sentry-sdk`, but only calls `sentry_sdk.init()` from its desktop GUI (`gui/qt.py`) and its own multiplayer server. The CLI never initialises it, so nothing is sent. |
| Image size | **202 MB → 634 MB.** Randovania itself is 117 MB (it bundles every game's data); numpy and its libraries about 68 MB. |
| Licenses | All compatible with GPL-3.0-or-later (GPL-3.0, LGPL-3.0, MIT, BSD, Apache-2.0). |
| Known vulnerabilities | `cryptography` 48.0.1 (pinned `<49` by Randovania) has three advisories, none reachable here. Resolved by decision 6. |
| arm64 | **Blocked for the image.** `py-tegra-swizzle` (needed by the Dread file-format library `mercury-engine-data-structures`) has never published an aarch64 Linux wheel. It ships a Rust source package, so `uv` accepts the lock, but our Docker build stage has no Rust toolchain. x86-64 is unaffected. |

## Decision

1. **No adapter module for now.** The CLI covers generation, permalinks,
   description and export, so the server calls `python -m randovania`
   subprocesses (ADR 0002's CLI-first path). Revisit only if a needed
   capability is missing from the CLI.
2. **Pin `randovania` and `open-dread-rando` exactly**, and update them only
   together: Dependabot groups them, and `tests/test_randovania_cli.py` fails if
   the Dread patcher version differs from the one Randovania's `exporters`
   extra pins.
3. **Keep the Phase 0 behaviour as regression tests:** headless generation,
   the export path up to the game-file boundary, and "no Qt installed".
4. **Accept the image size for now.** Trimming other games' data from the
   Randovania package would mean modifying a dependency in our build, which
   is fragile and makes the image harder to verify. Revisit if size becomes a
   problem.
5. **arm64 stays deferred (ADR 0003)** until the `py-tegra-swizzle` gap is
   closed. Options, in order of preference:
   1. Ask upstream (`py-tegra-swizzle`) to publish aarch64 Linux wheels; its
      build already produces other platforms' wheels.
   2. Build that one package from source in the Docker build stage when
      `TARGETARCH=arm64`, with a pinned Rust toolchain that never reaches the
      runtime image.
   3. Build and publish our own wheel from a pinned upstream release.

6. **Override Randovania's `cryptography<49` pin to `cryptography>=50`** (via
   `[tool.uv] override-dependencies`). Randovania 12.0.0's pin keeps
   `cryptography` 48.0.1, which pip-audit flags for CVE-2026-69247 (PKCS#7
   decryption oracle), CVE-2026-69248 (X.509 name-constraint escape) and
   CVE-2026-69249 (X.509 chain-building DoS). None is reachable here: nothing
   installed calls PKCS#7 decryption or X.509 verification, and Randovania's
   only use (Fernet obfuscation of race-seed spoilers) needs a secret that only
   official builds embed, so in our build it raises `MissingSecret`, which
   Randovania handles by skipping obfuscation. We override instead of
   suppressing the audit because fixing beats ignoring; with 50.0.2 every test
   passes and pip-audit is clean. A test fails once Randovania allows a fixed
   release itself, so the override is removed when it is no longer needed.
   (A side effect of the missing secret, independent of this choice:
   self-hosted builds can't obfuscate race-seed spoilers.)

## Consequences

- The weekly arm64 test build is expected to fail at the image build until
  option 5.1 or 5.2 lands. It will open a tracking issue, which is the
  intended behaviour.
- Generation takes tens of seconds and is CPU-bound, so the server must run it
  as a background job with progress updates (as planned in `docs/PLAN.md` §4), never
  inside a request.
- **On every Randovania upgrade, re-check:** the CLI commands and flags used
  here, the exporters-extra pin and the `cryptography` override (both enforced by tests), that `sentry_sdk.init()`
  is still only called from the GUI and server code, and the image size.
