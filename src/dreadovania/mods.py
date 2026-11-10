# SPDX-License-Identifier: GPL-3.0-or-later
"""Patched mods: checking, storing and zipping the patcher's output (ADR 0013 §3).

A mod is kept as a folder laid out like the SD card's ``atmosphere/`` folder
(``contents/<title id>/…`` and ``exefs_patches/…``), at
``seeds/<owner>/<seed>/mod/``. The zip download is streamed from that folder
with ``atmosphere/`` at the top, so it is extracted at the root of the SD card.

The mod is derived from the user's own game files: it is only ever sent to that
user, and its paths and contents are never logged.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import tarfile
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DREAD_TITLE_ID = "010093801237c000"
# Where Atmosphere loads the mod from; the patcher writes this layout.
REQUIRED_DIR = f"contents/{DREAD_TITLE_ID}"
ALLOWED_TOP_LEVEL = frozenset({"contents", "exefs_patches"})
# Written next to the mod by the patcher for non-race seeds. It describes the
# layout and isn't needed on the Switch, so it is never kept.
DROPPED_FILES = frozenset({"patcher.json"})
ZIP_ROOT = "atmosphere"
CHUNK = 1024 * 1024
_ZIP_EPOCH = 315532800 + 86400  # zip timestamps start in 1980


class ModError(Exception):
    """The patcher's output isn't usable. The message is safe to show to the user."""


@dataclass(frozen=True)
class ModInfo:
    file_count: int
    size_bytes: int


def walk_files(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Every file under root, sorted, without following symlinks.

    Anything other than a regular file or a directory is an error: the patcher
    never writes symlinks, devices or pipes.
    """
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(files) + [d for d in dirs if Path(dirpath, d).is_symlink()]:
            path = Path(dirpath, name)
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ModError("the patched files contain something other than plain files")
            yield path, st


def inspect_mod(root: Path) -> ModInfo:
    if not (root / REQUIRED_DIR).is_dir():
        raise ModError("the patcher didn't produce a mod for Atmosphere")
    unexpected = {p.name for p in root.iterdir()} - ALLOWED_TOP_LEVEL
    if unexpected:
        raise ModError("the patcher produced unexpected files")
    count = size = 0
    for _path, st in walk_files(root):
        count += 1
        size += st.st_size
    if count == 0:
        raise ModError("the patcher produced no files")
    return ModInfo(file_count=count, size_bytes=size)


def finalize_mod(output: Path, destination: Path) -> ModInfo:
    """Check the patcher's output and move it into place, replacing any older mod."""
    for name in DROPPED_FILES:
        (output / name).unlink(missing_ok=True)
    info = inspect_mod(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    old = None
    if destination.exists():
        old = destination.with_name(f".{destination.name}-old-{secrets.token_hex(4)}")
        os.replace(destination, old)
    os.replace(output, destination)
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    return info


def remove_mod(destination: Path) -> None:
    shutil.rmtree(destination, ignore_errors=True)


class _Sink:
    """A write-only, unseekable buffer: zipfile then streams with data descriptors."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def write(self, data: bytes) -> int:
        self._buffer += data
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


def iter_mod_zip(root: Path) -> Iterator[bytes]:
    """Stream the mod as a zip, ``atmosphere/…`` at the top, without a temporary file.

    Stored, not compressed: the game data barely compresses, and this keeps the
    server's CPU free. Sizes are only known as each file is written, so the zip
    uses data descriptors and ZIP64.
    """
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED) as archive:  # type: ignore[arg-type]
        for path, st in walk_files(root):
            info = zipfile.ZipInfo(
                f"{ZIP_ROOT}/{path.relative_to(root).as_posix()}",
                date_time=time.localtime(max(st.st_mtime, _ZIP_EPOCH))[:6],
            )
            info.external_attr = 0o644 << 16
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as entry:
                while chunk := source.read(CHUNK):
                    entry.write(chunk)
                    yield sink.take()
            yield sink.take()
    yield sink.take()


# ---- the Switch app's download: a manifest and an uncompressed tar (ADR 0018) ----------------

TAR_BLOCK = 512


def _tar_name(root: Path, path: Path) -> str:
    return f"{ZIP_ROOT}/{path.relative_to(root).as_posix()}"


def mod_manifest(root: Path) -> list[dict[str, object]]:
    """Every file the tar will contain, in order: SD card path, size and SHA-256."""
    files = []
    for path, st in walk_files(root):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(CHUNK):
                digest.update(chunk)
        files.append({"path": _tar_name(root, path), "size": st.st_size,
                      "sha256": digest.hexdigest()})  # fmt: skip
    return files


def _tar_header(name: str, size: int) -> bytes:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o644
    info.mtime = _ZIP_EPOCH
    try:
        # Plain ustar: names up to 255 characters, split into prefix and name, so
        # the Switch app's reader needs no PAX or GNU extensions.
        return info.tobuf(format=tarfile.USTAR_FORMAT)
    except ValueError:
        raise ModError("a patched file's name is too long to send to the Switch") from None


def tar_size(root: Path) -> int:
    """The exact length of iter_mod_tar's output, for Content-Length."""
    total = 2 * TAR_BLOCK  # end-of-archive marker
    for _path, st in walk_files(root):
        total += TAR_BLOCK + -(-st.st_size // TAR_BLOCK) * TAR_BLOCK
    return total


def iter_mod_tar(root: Path) -> Iterator[bytes]:
    """Stream the mod as an uncompressed ustar archive, ``atmosphere/…`` at the top."""
    for path, st in walk_files(root):
        yield _tar_header(_tar_name(root, path), st.st_size)
        written = 0
        with path.open("rb") as source:
            while chunk := source.read(CHUNK):
                written += len(chunk)
                yield chunk
        if written != st.st_size:
            raise ModError("a patched file changed while it was being sent")
        if written % TAR_BLOCK:
            yield bytes(TAR_BLOCK - written % TAR_BLOCK)
    yield bytes(2 * TAR_BLOCK)
