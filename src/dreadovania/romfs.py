# SPDX-License-Identifier: GPL-3.0-or-later
"""The user's game files: an extracted RomFS in one folder (ADR 0013 §1).

This module only *inspects* the folder. It never serves, copies or logs its
contents or paths, and it never handles game packages or console keys: only an
already-extracted RomFS is accepted.

The game version is identified from the MD5 of ``system/files.toc``, using the
table in ``mercury-engine-data-structures`` (the library the Dread patcher
uses), so no hashes are copied into this repository.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mercury_engine_data_structures.game_check import Game, GameVersion

TOC = "system/files.toc"
# The same files Randovania's own export dialog checks for in a Dread RomFS.
REQUIRED_FILES = (
    "config.ini",
    TOC,
    "packs/system/system.pkg",
    "packs/maps/s010_cave/s010_cave.pkg",
    "packs/maps/s020_magma/s020_magma.pkg",
)
# Written by the patcher: a RomFS containing it has already been randomized.
PATCHED_MARKER = "custom_names.json"
# Randovania 12 exports Dread from these versions only (its export dialog says so).
SUPPORTED_VERSIONS = ("1.0.0", "2.1.0")
# Game packages are not accepted: unpacking one needs console keys (ADR 0013 §1).
PACKAGE_SUFFIXES = frozenset({".nsp", ".xci", ".nsz", ".xcz", ".nca"})

MAX_TOC_BYTES = 64 * 1024 * 1024
MAX_FILES = 200_000

State = Literal[
    "missing", "package", "not_romfs", "nested", "unreadable", "modified",
    "unknown_version", "unsupported_version", "ready",
]  # fmt: skip

# Takes the contents of system/files.toc; returns a Dread version string or None.
Identify = Callable[[bytes], "str | None"]


@dataclass(frozen=True)
class RomfsStatus:
    state: State
    message: str
    version: str | None = None
    file_count: int | None = None
    total_bytes: int | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def dread_versions_by_toc_md5() -> dict[bytes, str]:
    """The library's table of Dread versions, keyed by the MD5 of files.toc."""
    return {v.toc_hash: v.as_string for v in GameVersion if v.game == Game.DREAD}


def identify_by_toc(toc: bytes) -> str | None:
    """Identify a Dread version from files.toc, or None if it isn't a known one."""
    digest = hashlib.md5(toc, usedforsecurity=False).digest()
    return dread_versions_by_toc_md5().get(digest)


class RomfsInspector:
    """Reports on the RomFS folder. Cheap enough to run on every request."""

    def __init__(self, root: Path, identify: Identify = identify_by_toc) -> None:
        self.root = root
        self._identify = identify

    def status(self) -> RomfsStatus:
        try:
            return self._inspect()
        except PermissionError:
            return RomfsStatus(
                "unreadable",
                "The server can't read the game files folder. Make it readable by the "
                "container's user (UID 10001), then check again.",
            )
        except OSError:
            return RomfsStatus("unreadable", "The game files folder couldn't be read.")

    def _inspect(self) -> RomfsStatus:
        root = self.root
        if not root.is_dir() or not any(root.iterdir()):
            return RomfsStatus(
                "missing", "No game files yet. Put an extracted RomFS in the game files folder."
            )
        if any(p.suffix.lower() in PACKAGE_SUFFIXES for p in root.iterdir()):
            return RomfsStatus(
                "package",
                "This looks like a game package, not an extracted RomFS. Dreadovania only "
                "accepts the extracted RomFS folder: dump it from your own game with a "
                "homebrew dumping tool, then put its contents here.",
            )
        missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
        if missing:
            if _single_nested_romfs(root):
                return RomfsStatus(
                    "nested",
                    "The RomFS is one folder too deep. Move the contents of that folder up, "
                    "so that `system/` and `packs/` sit directly in the game files folder.",
                )
            return RomfsStatus(
                "not_romfs",
                "This isn't a complete Dread RomFS. Missing: " + ", ".join(missing) + ".",
            )
        if (root / PATCHED_MARKER).exists():
            return RomfsStatus(
                "modified",
                "These game files have already been randomized. Use a clean RomFS dumped "
                "from your own game.",
            )
        toc = root / TOC
        if toc.stat().st_size > MAX_TOC_BYTES:
            return RomfsStatus("not_romfs", "system/files.toc is too large to be a Dread RomFS.")
        version = self._identify(toc.read_bytes())
        count, size = _count_files(root)
        if count is None:
            return RomfsStatus("not_romfs", "This folder has far too many files to be a RomFS.")
        if version is None:
            return RomfsStatus(
                "unknown_version",
                "This doesn't match any known version of Dread. Dump the RomFS again from "
                "your own game, without changes.",
                file_count=count,
                total_bytes=size,
            )
        if version not in SUPPORTED_VERSIONS:
            return RomfsStatus(
                "unsupported_version",
                f"This is version {version}, which can't be patched. Supported: "
                f"{' and '.join(SUPPORTED_VERSIONS)}. Update the game to "
                f"{SUPPORTED_VERSIONS[-1]} and dump the RomFS again.",
                version=version,
                file_count=count,
                total_bytes=size,
            )
        return RomfsStatus(
            "ready",
            f"Ready: Dread {version}.",
            version=version,
            file_count=count,
            total_bytes=size,
        )


def _single_nested_romfs(root: Path) -> bool:
    entries = [p for p in root.iterdir() if not p.name.startswith(".")]
    return (
        len(entries) == 1
        and entries[0].is_dir()
        and all((entries[0] / name).is_file() for name in REQUIRED_FILES)
    )


def _count_files(root: Path) -> tuple[int | None, int]:
    """Regular files and their total size, without following symlinks."""
    count = size = 0
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            st = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISREG(st.st_mode):
                count += 1
                size += st.st_size
                if count > MAX_FILES:
                    return None, size
    return count, size
