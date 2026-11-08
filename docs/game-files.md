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

There are two ways: upload a zip from the web UI, or mount a folder. Mounting is
faster for a multi-GB RomFS if you can copy files to the server directly.

### Uploading a zip

On **Game files**, choose a zip of the RomFS and press **Upload and install**.
`system/` and `packs/` can be at the top of the zip or inside one folder.

- The upload is sent in chunks and can be resumed: if it stops (the phone sleeps,
  the connection drops), choose the same file again and it continues where it left
  off. Unfinished uploads are discarded after a week.
- When it's complete, the server checks every entry (no paths outside the folder,
  no links, size and file-count limits), extracts it, and checks the result
  exactly like a mounted folder. Only a supported, unmodified RomFS replaces your
  current game files.
- It needs free space for the zip and the extracted files while it installs,
  plus the old game files until the swap.
- Windows' built-in "Compressed folder" uses Deflate64 for large files, which the
  server can't read. Use 7-Zip (or any tool with standard Deflate, or no
  compression).
- Behind a reverse proxy, allow request bodies of at least the chunk size (8 MB by
  default; nginx's default limit is 1 MB): for example `client_max_body_size 16m;`.
  Or lower the chunk size with `DREADOVANIA_ROMFS_UPLOAD_CHUNK_BYTES`.
- Limits: `DREADOVANIA_MAX_ROMFS_UPLOAD_BYTES` (default 20 GiB) and
  `DREADOVANIA_MAX_ROMFS_FILES` (default 100,000).

Uploading is turned off while the game files folder is a mount: replace the files
on the host instead.

### Mounting a folder

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
