# SPDX-License-Identifier: GPL-3.0-or-later
"""Installing a patched seed on a Switch over FTP (ADR 0016, Phase 3 PR 2).

The seed is installed directly into Atmosphere's folders. A seed holds only the
files it changes, so the previous seed's files must go first, or two seeds would
mix. Other mods are kept: Dreadovania removes only the files it installed itself,
which it records in a manifest on the server and on the SD card.

Safety rules:

- Nothing outside Dread's two mod folders (``MOD_ROOTS``) is ever deleted, even
  if a manifest says otherwise (the SD card's copy can be edited by anyone).
- A file that Dreadovania didn't install is never overwritten unless the user
  asks (``overwrite``); the send stops and lists such files instead.
- The manifest of planned files is recorded before anything changes on the
  card, so an interrupted send is still cleaned up by the next one.

Runs in a worker thread with a blocking ``ftplib.FTP``; ``cancelled`` is checked
between files.
"""

from __future__ import annotations

import contextlib
import ftplib
import io
import json
import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dreadovania.mods import walk_files

DREAD_TITLE_ID = "010093801237c000"
MOD_ROOTS = (
    f"/atmosphere/contents/{DREAD_TITLE_ID}",
    "/atmosphere/exefs_patches/DreadRandovania",
)
# Never remove these, even when empty: other mods and Atmosphere use them.
_KEEP_DIRS = frozenset({"/", "/atmosphere", "/atmosphere/contents", "/atmosphere/exefs_patches"})
SD_MANIFEST = "/config/dreadovania/installed.json"
BLOCK = 64 * 1024
MAX_LISTED = 10


class PushError(Exception):
    """The send failed. The message is safe to show to the user."""


class Conflict(PushError):
    def __init__(self, files: list[str]) -> None:
        shown = ", ".join(files[:MAX_LISTED]) + (" …" if len(files) > MAX_LISTED else "")
        super().__init__(
            f"{len(files)} file(s) on the Switch belong to another mod and would be replaced: "
            f'{shown}. Send again with "Replace other mods\' files" to replace them, or '
            "remove that mod first"
        )
        self.files = files


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class PushOptions:
    overwrite: bool = False  # replace files that another mod installed
    clear: bool = False  # empty Dread's mod folders first, whatever is in them


@dataclass(frozen=True)
class Progress:
    files_done: int
    files_total: int
    bytes_done: int
    bytes_total: int

    def describe(self) -> str:
        mb = 1024 * 1024
        return (
            f"Sending file {min(self.files_done + 1, self.files_total)} of {self.files_total} "
            f"({self.bytes_done / mb:.1f} of {self.bytes_total / mb:.1f} MB)"
        )


def in_mod_roots(path: str) -> bool:
    path = posixpath.normpath(path)
    return any(path.startswith(root + "/") for root in MOD_ROOTS) and ".." not in path.split("/")


def plan(mod_dir: Path) -> list[tuple[Path, str, int]]:
    """Each file of the mod: (local path, SD card path, size)."""
    files = []
    for path, st in walk_files(mod_dir):
        rel = path.relative_to(mod_dir).as_posix()
        if any(c in rel for c in "\r\n\0"):
            raise PushError("the patched files have a name that can't be sent over FTP")
        remote = f"/atmosphere/{rel}"
        if not in_mod_roots(remote):
            raise PushError(f"the patched files include {remote}, outside Dread's mod folders")
        files.append((path, remote, st.st_size))
    if not files:
        raise PushError("the patched seed has no files")
    return files


def read_sd_manifest(ftp: ftplib.FTP) -> list[str]:
    """The files the card says Dreadovania installed; anything odd is ignored."""
    buffer = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {SD_MANIFEST}", buffer.write)
        data = json.loads(buffer.getvalue().decode("utf-8"))
    except (ftplib.error_perm, ValueError, UnicodeDecodeError):
        return []
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, str) and in_mod_roots(f)]


def write_sd_manifest(
    ftp: ftplib.FTP, files: list[str], seed_id: str | None, state: str, made: set[str]
) -> None:
    ensure_dir(ftp, posixpath.dirname(SD_MANIFEST), made)
    body = json.dumps(
        {"tool": "dreadovania", "seed_id": seed_id, "state": state, "files": files}, indent=1
    )
    ftp.storbinary(f"STOR {SD_MANIFEST}", io.BytesIO(body.encode("utf-8")))


def listing(ftp: ftplib.FTP, directory: str) -> set[str]:
    """Full paths of the entries in a directory (empty if it doesn't exist)."""
    try:
        names = ftp.nlst(directory)
    except ftplib.error_perm:
        return set()
    # Servers return either bare names or full paths.
    return {posixpath.join(directory, posixpath.basename(name.rstrip("/"))) for name in names}


def ensure_dir(ftp: ftplib.FTP, directory: str, made: set[str]) -> None:
    current = ""
    for part in directory.strip("/").split("/"):
        current = f"{current}/{part}"
        if current in made:
            continue
        with contextlib.suppress(ftplib.error_perm):  # already there
            ftp.mkd(current)
        made.add(current)


def remove_tree(ftp: ftplib.FTP, root: str, depth: int = 0) -> None:
    """Delete everything under a mod root (only ever called on MOD_ROOTS)."""
    if depth > 32:
        raise PushError("the mod folder on the Switch is nested too deeply to clear")
    for entry in sorted(listing(ftp, root)):
        try:
            ftp.delete(entry)
        except ftplib.error_perm:
            remove_tree(ftp, entry, depth + 1)  # a directory
    with contextlib.suppress(ftplib.error_perm):
        ftp.rmd(root)


def remove_files(ftp: ftplib.FTP, files: list[str]) -> None:
    """Delete files Dreadovania installed, then any of their folders left empty."""
    dirs: set[str] = set()
    for path in files:
        if not in_mod_roots(path):
            continue  # never outside Dread's mod folders, whatever a manifest says
        with contextlib.suppress(ftplib.error_perm):  # already gone
            ftp.delete(path)
        parent = posixpath.dirname(path)
        while parent not in _KEEP_DIRS and parent not in dirs:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    for directory in sorted(dirs, key=lambda d: d.count("/"), reverse=True):
        # Fails, harmlessly, if another mod's files are in it.
        with contextlib.suppress(ftplib.error_perm):
            ftp.rmd(directory)


def push(
    ftp: ftplib.FTP,
    mod_dir: Path,
    *,
    seed_id: str,
    previous: list[str],
    options: PushOptions,
    record_planned: Callable[[list[str]], None],
    on_progress: Callable[[Progress], None],
    cancelled: Callable[[], bool],
) -> list[str]:
    """Install the mod; returns the SD card paths now owned by Dreadovania."""
    files = plan(mod_dir)
    planned = [remote for _local, remote, _size in files]
    planned_set = set(planned)
    owned = {p for p in previous if in_mod_roots(p)} | set(read_sd_manifest(ftp))

    if not options.clear:
        present: set[str] = set()
        for directory in sorted({posixpath.dirname(p) for p in planned}):
            present |= listing(ftp, directory)
        conflicts = sorted(p for p in planned if p in present and p not in owned)
        if conflicts and not options.overwrite:
            raise Conflict(conflicts)

    # From here on the card changes: record what Dreadovania will own first.
    made: set[str] = set()
    pending = sorted(owned | planned_set)
    record_planned(pending)
    write_sd_manifest(ftp, pending, seed_id, "installing", made)

    if options.clear:
        for root in MOD_ROOTS:
            remove_tree(ftp, root)
        made.clear()  # folders may be gone now
    else:
        remove_files(ftp, sorted(owned - planned_set))

    total = sum(size for _l, _r, size in files)
    done_bytes = 0
    for index, (local, remote, _size) in enumerate(files):
        if cancelled():
            raise Cancelled
        ensure_dir(ftp, posixpath.dirname(remote), made)
        progress = Progress(index, len(files), done_bytes, total)
        on_progress(progress)

        def count(block: bytes) -> None:
            nonlocal done_bytes
            done_bytes += len(block)

        with local.open("rb") as source:
            ftp.storbinary(f"STOR {remote}", source, blocksize=BLOCK, callback=count)
    on_progress(Progress(len(files), len(files), total, total))

    write_sd_manifest(ftp, sorted(planned), seed_id, "installed", made)
    return sorted(planned)
