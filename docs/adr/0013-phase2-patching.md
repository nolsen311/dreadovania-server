# 0013. Phase 2 plan: patching seeds with the user's own game files

- Status: Accepted
- Date: 2026-11-08
- Builds on ADR 0001 (Dread only), 0010 (CLI findings), 0011 (Phase 1).
- Patching comes before TLS and passkeys (ADR 0011 PR 5), at the maintainer's request.

## Context

Phase 1 generates seeds. To play one, the seed must be applied to the user's
own copy of the game, producing a mod that Atmosphere loads. Phase 0 showed the
export runs headless through the CLI until it reads the user's RomFS
(ADR 0010). These facts come from Randovania 12.0.0 and its Dread libraries:

- The exporter needs **an extracted RomFS** ("a dumped RomFS of your original
  game. Either version 1.0.0 or 2.1.0"). It does not take the ExeFS: the patcher
  generates its own ExeFS changes.
- The game version is identified from the MD5 of `system/files.toc`, against a
  table in `mercury-engine-data-structures` (`GameVersion`): Dread 1.0.0, 1.0.1,
  2.0.0 and 2.1.0. Randovania supports **1.0.0 and 2.1.0**.
- With `--target-platform atmosphere`, the output mirrors the SD card's
  `atmosphere/` folder: `contents/010093801237c000/{romfs,exefs}` and
  `exefs_patches/DreadRandovania/`.

## Decision

### 1. Game files: an extracted RomFS, in one place

- The server reads the RomFS from **`/data/romfs`**. Two ways to fill it, the
  maintainer's choice:
  - **Mount** a host folder there (read-only recommended).
  - **Upload** a zip of the RomFS from the web UI (PR 3 below). Uploading is
    disabled when `/data/romfs` is a read-only mount.
- **Only an extracted RomFS is accepted.** The server never takes `.nsp`/`.xci`
  files and never handles console keys: turning a game file into a RomFS needs
  decryption keys, which stay out of this project entirely (content policy,
  `docs/PLAN.md` §3). Users dump their RomFS with their own homebrew tools.
- **Validation:** the server identifies the version from `system/files.toc`
  using the library's own table (no hashes are copied into this repository),
  accepts **1.0.0 and 2.1.0**, and explains anything else (not a Dread RomFS;
  a known but unsupported version, with what to do).
- The RomFS is never served, listed in detail, logged, or included in any
  backup or download. Its status (present, version, file count, size) is all
  the API exposes.

### 2. Patching: a new job kind, on demand

- A ready seed gets a **"Patch for Switch"** action. It queues an `export` job
  in the existing single-worker queue (ADR 0011 §2): `layout patcher-data`, then
  `layout export-game … dread --target-platform atmosphere --input-path
  /data/romfs --output-path <work dir>`, with a longer timeout than generation.
- Race seeds can be patched; patching doesn't need or reveal the spoiler.
- Patching is refused, with a clear message, if the RomFS is missing or invalid.

### 3. The result: a folder and a zip

- The output is kept as a **folder** (`/data/seeds/<owner>/<seed>/mod/`, laid
  out like the SD card's `atmosphere/`), which Phase 3's FTP push will reuse,
  and offered as a **zip** download built from it (`atmosphere/…` at the top, so
  it is extracted at the SD card's root).
- Deleting or purging a seed deletes its mod too. A seed's detail page shows
  whether it has been patched, the size, and the game version used.
- The mod contains files derived from the user's own game. It never leaves the
  user's server except when they download it.

### 4. Pull requests, in order

1. **Game files status and validation** (mounted folder): `GET /api/v1/romfs`,
   a "Game files" page in the UI, version detection, clear errors.
2. **Patching**: the `export` job kind, the seed action, mod folder and zip
   download, deletion and purge of mods.
3. **Upload**: a resumable, chunked upload of a RomFS zip to `/data/romfs`, with
   safe extraction (no paths outside the target, size and file-count limits),
   validation before it replaces anything, and progress in the UI.

## Consequences

- **Real patching can't be tested in CI or in Claude's sessions:** that needs a
  real RomFS, which never enters this project. Tests use a synthetic RomFS with
  version detection injected, and a fake runner; the real path is verified by
  the maintainer on their own server before each Phase 2 PR is merged, using a
  short checklist in the PR.
- A patched mod is a few hundred MB (corrected: typically tens of MB, as the
  patcher writes only changed assets; see ADR 0016); disk use grows with patched seeds. The
  seed list shows mod sizes, and purge removes them.
- Uploading a multi-gigabyte RomFS from a phone is slow; mounting stays the
  recommended way.
