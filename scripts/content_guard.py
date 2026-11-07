"""Reject commits that contain game content, console keys or large binaries.

This repository must never contain copyrighted game data (docs/PLAN.md section 3).
The guard checks every git-tracked file by name, by location and by content, so a
renamed dump is caught as well as an obvious one.

Usage: python scripts/content_guard.py [ROOT]
Exits non-zero and lists every offending file if anything is found.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Switch container and archive formats, plus common disc and cartridge image formats.
BLOCKED_SUFFIXES = frozenset(
    {
        ".nsp", ".nsz", ".xci", ".xcz", ".nca", ".ncz", ".nso", ".npdm",
        ".iso", ".wbfs", ".rvz", ".gcm", ".ciso", ".wua", ".wud", ".rpx",
        ".pfs0", ".hfs0", ".romfs", ".bfsar",
        # Dread's packed asset formats (should only ever come from a user's own dump).
        ".pkg", ".bmsad", ".bmsld", ".bmscc", ".bmdefs", ".bctex", ".bcmdl",
    }
)  # fmt: skip

# Key files used to decrypt console content.
BLOCKED_NAMES = frozenset({"prod.keys", "title.keys", "dev.keys", "console.keys"})
BLOCKED_NAME_SUFFIXES = (".keys",)

# Extracted filesystem trees from a game dump.
BLOCKED_DIRS = frozenset({"romfs", "exefs"})

# Magic bytes at offset 0 that identify Switch content regardless of the file name.
BLOCKED_MAGIC = {
    b"PFS0": "Switch PFS0 container (NSP)",
    b"HFS0": "Switch HFS0 partition",
    b"NSO0": "Switch NSO executable",
    b"META": "Switch NPDM metadata",
}
# XCI cartridge images carry "HEAD" at offset 0x100.
XCI_MAGIC_OFFSET = 0x100
XCI_MAGIC = b"HEAD"

MAX_FILE_BYTES = 5 * 1024 * 1024

# Files allowed to exceed the size limit. Keep this list short and justified.
SIZE_ALLOWLIST = frozenset({"uv.lock"})


@dataclass(frozen=True)
class Finding:
    path: str
    reason: str


def check_file(root: Path, rel: str) -> list[Finding]:
    findings: list[Finding] = []
    pure = PurePosixPath(rel)
    name = pure.name.lower()

    if pure.suffix.lower() in BLOCKED_SUFFIXES:
        findings.append(Finding(rel, f"blocked file type '{pure.suffix}'"))
    if name in BLOCKED_NAMES or name.endswith(BLOCKED_NAME_SUFFIXES):
        findings.append(Finding(rel, "console key file"))
    if any(part.lower() in BLOCKED_DIRS for part in pure.parts[:-1]):
        findings.append(Finding(rel, "inside an extracted game filesystem directory"))

    path = root / rel
    if path.is_symlink() or not path.is_file():
        return findings

    size = path.stat().st_size
    if size > MAX_FILE_BYTES and rel not in SIZE_ALLOWLIST:
        findings.append(Finding(rel, f"file is {size} bytes, over the {MAX_FILE_BYTES} limit"))

    with path.open("rb") as fh:
        head = fh.read(XCI_MAGIC_OFFSET + len(XCI_MAGIC))
    for magic, label in BLOCKED_MAGIC.items():
        if head.startswith(magic):
            findings.append(Finding(rel, f"content looks like a {label}"))
    if head[XCI_MAGIC_OFFSET : XCI_MAGIC_OFFSET + len(XCI_MAGIC)] == XCI_MAGIC:
        findings.append(Finding(rel, "content looks like an XCI cartridge image"))

    return findings


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def scan(root: Path, files: Iterable[str]) -> list[Finding]:
    return [finding for rel in files for finding in check_file(root, rel)]


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    findings = scan(root, tracked_files(root))
    if not findings:
        print("content guard: no game content, keys or large binaries found")
        return 0
    for f in findings:
        # GitHub Actions annotation format, still readable in a terminal.
        print(f"::error file={f.path}::{f.reason}")
    print(
        f"content guard: {len(findings)} problem(s). This repository must never contain "
        "game content or keys; see docs/PLAN.md section 3.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
