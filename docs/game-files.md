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

There are two ways: upload an archive from the web UI, or mount a folder. Mounting is
faster for a multi-GB RomFS if you can copy files to the server directly.

### Uploading an archive

On **Game files**, choose an archive of the RomFS and press **Upload and install**.
It can be a `.zip`, or a `.tar`, `.tar.gz`, `.tar.bz2` or `.tar.xz` (for example
`tar -czf romfs.tar.gz -C /path/to/romfs .`). The format is recognised from the
file's content. `system/` and `packs/` can be at the top of the archive or inside
one folder. 7z and RAR aren't supported: 7z would need extra third-party code, and
RAR's decoder isn't free software.

- The upload is sent in chunks and can be resumed: if it stops (the phone sleeps,
  the connection drops), choose the same file again and it continues where it left
  off. Unfinished uploads are discarded after a week.
- When it's complete, the server checks every entry (no paths outside the folder,
  no links, size and file-count limits), extracts it, and checks the result
  exactly like a mounted folder. Only a supported, unmodified RomFS replaces your
  current game files.
- It needs free space for the archive and the extracted files while it installs,
  plus the old game files until the swap.
- Windows' built-in "Compressed folder" uses Deflate64 for large files, which the
  server can't read. Use 7-Zip with standard Deflate, or Windows' own `tar`
  (Windows 10 and later): `tar -czf romfs.tar.gz -C C:\path\to\romfs .`
- Patching and installing never overlap: an install isn't started while a seed is
  being patched, and a patch doesn't start while new game files are installed.
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

## Patching a seed

On a generated seed's page, choose **Patch for Switch**. Patching takes a few
minutes and runs in the same queue as generation, one job at a time. Race seeds
can be patched too: patching never shows the spoiler.

When it finishes, **Download for Switch (.zip)** and extract the zip at the root
of your SD card. It contains an `atmosphere/` folder:

- `atmosphere/contents/010093801237c000/` holds the changed game files and a
  small loader (`exefs/`), and
- `atmosphere/exefs_patches/DreadRandovania/` may hold code patches.

**Before installing a new seed by hand, delete the previous seed's files**
(`atmosphere/contents/010093801237c000/` and `atmosphere/exefs_patches/DreadRandovania/`).
A seed contains only the files it changes, so extracting over an older seed can
leave some of its files behind and mix the two. Start a new save file for each
seed. Sending a seed to the Switch from the server (Phase 3, ADR 0016) does this
cleanup for you.

The patched files stay on the server, next to the seed, until you delete them
(**Delete patched files**), delete the seed, or purge old seeds. A patched seed
is typically tens of MB: the patcher writes only the assets the seed changes,
which the loader reads ahead of the game's own packages.

## Sending a seed to the Switch

Copying a mod over MTP is slow, because a mod is many small files. Instead, the
server can install it over Wi-Fi through **ftpd** on the Switch (ADR 0016):

1. **Once:** on **Switches**, add your Switch with the address and port ftpd
   shows (port 5000 by default), and press **Test connection** with ftpd open.
   Only addresses on your own network are accepted, because FTP isn't encrypted.
   Give the Switch a fixed address in your router (a DHCP reservation) so it
   keeps working.
2. **Each seed:** open ftpd on the Switch. On the patched seed's page, under
   **Send to Switch**, choose the Switch and press **Send to Switch**.
3. When it says **Sent**, close ftpd and launch Dread with a new save file.

**Firewalls:** besides its own port (5000), ftpd opens a new, random port on the
Switch for every file it sends or receives (passive mode). If a firewall sits
between the server and the Switch, for example when the Switch is on a separate
VLAN, allow TCP from the server's host to the Switch on ports 1024–65535.
Allowing only port 5000 lets ftpd log in but not transfer files; **Test
connection** checks both.

**What a send changes on the SD card:**
- It removes the files the previous Dreadovania seed installed, then copies
  this seed into `atmosphere/contents/010093801237c000/` (and
  `atmosphere/exefs_patches/DreadRandovania/`).
- It records the files it installed in `/config/dreadovania/installed.json`, so
  the next send (from this or another server) can remove exactly those.
- **Other mods are kept.** If this seed would replace a file that another mod
  installed, the send stops and lists the files. Under **Options**, **Replace
  other mods' files** replaces them, and **Empty Dread's mod folder first**
  removes everything in Dread's mod folders, other Dread mods included.
- If a send is interrupted (cancelled, or the connection drops), the next send
  cleans up what was copied.

 (default 1800).
