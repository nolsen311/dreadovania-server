<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Game files

Generating a seed needs no game files. **Patching** one, so that you can play
it, needs the files of your own copy of the game ([ADR 0013](adr/0013-phase2-patching.md)).

## What you need

- The **extracted RomFS** of your own copy, version **1.0.0 or 2.1.0**. Dump it
  on your Switch with a homebrew dumping tool, with the update applied if you
  are on 2.1.0.
- Not a game package: `.nsp`, `.xci` and similar files are refused, and
  Dreadovania never asks for or handles console keys.
- An unmodified RomFS: one that has already been randomized is refused.

## Adding them

Copy the RomFS folder to the machine running the server, then mount it at
`/data/romfs` (read-only is fine and recommended). `system/` and `packs/` must be
directly inside the mounted folder.

```yaml
volumes:
  - ./data:/data
  - /path/to/dread-romfs:/data/romfs:ro
```

The container runs as UID 10001, so the folder must be readable by that user.
To use a different path inside the container, set `DREADOVANIA_ROMFS_DIR`.

Open **Game files** in the web UI (or `GET /api/v1/romfs`) to check the result.
You will see one of these:

| Status | Meaning |
|---|---|
| Ready | A supported, unmodified RomFS. |
| Not set up | The folder is missing or empty. |
| Game package | A `.nsp`/`.xci` was found; extract the RomFS instead. |
| One folder too deep | Move the contents up so `system/` is at the top. |
| Not a complete RomFS | Required files are missing; the message lists them. |
| Already randomized | Use a clean dump. |
| Unknown version | Doesn't match any known Dread release; dump again. |
| Unsupported version | A known version that can't be patched; update to 2.1.0. |
| Can't read | Fix the folder's permissions for UID 10001. |

The version is identified the same way the Dread patcher does it: from the MD5 of
`system/files.toc`, using the table in `mercury-engine-data-structures`.

## Privacy

Your game files never leave your server. The server only reports their status
(version, number of files, total size): it never serves, lists or logs their
contents or paths.

## Patching a seed

On a generated seed's page, choose **Patch for Switch**. Patching takes a few
minutes and runs in the same queue as generation, one job at a time. Race seeds
can be patched too: patching never shows the spoiler.

When it finishes, **Download for Switch (.zip)** and extract the zip at the root
of your SD card. It contains an `atmosphere/` folder:

- `atmosphere/contents/010093801237c000/` holds the patched game files, and
- `atmosphere/exefs_patches/DreadRandovania/` holds the code patches.

Installing a seed replaces whichever seed was installed before, so start a new
save file for each seed. The patched files stay on the server, next to the seed,
until you delete them (**Delete patched files**), delete the seed, or purge old
seeds. Each patched seed takes a few hundred MB.

Patching is limited to `DREADOVANIA_EXPORT_TIMEOUT_SECONDS` (default 1800).
