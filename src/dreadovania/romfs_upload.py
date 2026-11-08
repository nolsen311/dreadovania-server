# SPDX-License-Identifier: GPL-3.0-or-later
"""Uploading a RomFS archive from the browser (ADR 0013 §1, Phase 2 PR 3).

A RomFS is several GB, so the upload is resumable: the browser sends the file in
chunks, each at an explicit offset, and the server's copy of the file is the
record of how much has arrived. An interrupted upload continues where it stopped.

The archive may be a zip or a tar (plain, gzip, bzip2 or xz), recognised from
its content. Once complete, it is extracted into a staging folder, with every entry
checked first (no paths outside the target, no links, size and file-count
limits). The result is validated like a mounted RomFS, and only a valid one
replaces the current game files, by renaming.

There is one upload at a time, for the whole server: the game files are shared.
Uploading is refused when the game files folder is a mount (replace it on the
host instead). File names and paths are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import lzma
import os
import secrets
import shutil
import stat
import tarfile
import time
import zipfile
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import IO, Literal

from dreadovania.romfs import PACKAGE_SUFFIXES, TOC, RomfsInspector

log = logging.getLogger(__name__)

State = Literal["receiving", "received", "installing", "failed", "installed"]
SUPPORTED_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
)
STALE_AFTER_SECONDS = 7 * 24 * 3600
SPACE_MARGIN_BYTES = 256 * 1024 * 1024
COPY_CHUNK = 1024 * 1024


class UploadError(Exception):
    """The request can't be done. The message is safe to show to the user."""


class UploadNotFound(UploadError):
    pass


class OffsetMismatch(UploadError):
    def __init__(self, expected: int) -> None:
        super().__init__(f"the upload continues at byte {expected}")
        self.expected = expected


@dataclass(frozen=True)
class UploadStatus:
    id: str
    state: State
    size: int
    offset: int
    chunk_size: int
    message: str | None = None
    installed_bytes: int = 0
    total_bytes: int | None = None


@dataclass
class _Meta:
    id: str
    size: int
    fingerprint: str  # a hash of what the browser sent: never the file name itself
    state: State
    message: str | None
    updated_at: float


class RomfsUploads:
    def __init__(
        self,
        data_dir: Path,
        romfs: RomfsInspector,
        *,
        max_bytes: int,
        max_files: int,
        chunk_size: int,
        is_busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.dir = data_dir / "uploads" / "romfs"
        self.romfs = romfs
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.chunk_size = chunk_size
        # True while something (a patch) is reading the game files: don't swap them then.
        self.is_busy = is_busy
        self._lock = asyncio.Lock()
        self._progress: tuple[int, int | None] = (0, None)
        self._task: asyncio.Task[None] | None = None

    # ---- paths and metadata -------------------------------------------------------

    @property
    def _meta_file(self) -> Path:
        return self.dir / "upload.json"

    @property
    def _archive(self) -> Path:
        return self.dir / "upload.archive"

    @property
    def _staging(self) -> Path:
        return self.dir / "staging"

    def _read_meta(self) -> _Meta | None:
        try:
            return _Meta(**json.loads(self._meta_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def _write_meta(self, meta: _Meta) -> None:
        meta.updated_at = time.time()
        tmp = self._meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(meta)), encoding="utf-8")
        os.replace(tmp, self._meta_file)

    def _offset(self) -> int:
        try:
            return self._archive.stat().st_size
        except FileNotFoundError:
            return 0

    def _status(self, meta: _Meta) -> UploadStatus:
        installed, total = self._progress if meta.state == "installing" else (0, None)
        return UploadStatus(
            id=meta.id,
            state=meta.state,
            size=meta.size,
            offset=meta.size if meta.state in ("installing", "installed") else self._offset(),
            chunk_size=self.chunk_size,
            message=meta.message,
            installed_bytes=installed,
            total_bytes=total,
        )

    # ---- availability -------------------------------------------------------------------

    def unavailable_reason(self) -> str | None:
        """Why uploading can't replace the game files here, or None if it can."""
        target = self.romfs.root
        if os.path.ismount(target):
            return (
                "The game files folder is mounted from the host. Replace the files there, "
                "or remove the mount to upload from this page."
            )
        parent = target.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            self.dir.mkdir(parents=True, exist_ok=True)
            same_disk = parent.stat().st_dev == self.dir.stat().st_dev
        except OSError:
            return "The server can't write to its data folder."
        if not same_disk:
            return "The game files folder is on a different disk from the server's data folder."
        if not os.access(parent, os.W_OK) or (target.exists() and not os.access(target, os.W_OK)):
            return "The server can't write to the game files folder."
        return None

    def _require_available(self) -> None:
        reason = self.unavailable_reason()
        if reason is not None:
            raise UploadError(reason)

    # ---- lifecycle ------------------------------------------------------------------------

    def recover(self) -> None:
        """At startup: an install that was running is lost; forget stale uploads."""
        shutil.rmtree(self._staging, ignore_errors=True)
        meta = self._read_meta()
        if meta is None:
            return
        if time.time() - meta.updated_at > STALE_AFTER_SECONDS:
            self._discard_files()
            return
        if meta.state == "installing":
            meta.state = "received" if self._offset() == meta.size else "receiving"
            meta.message = "The server restarted during the install. Install again."
            self._write_meta(meta)

    def _discard_files(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- the protocol -------------------------------------------------------------------

    @property
    def installing(self) -> bool:
        meta = self._read_meta()
        return meta is not None and meta.state == "installing"

    def current(self) -> UploadStatus | None:
        meta = self._read_meta()
        return self._status(meta) if meta is not None else None

    async def start(self, size: int, fingerprint: str) -> tuple[UploadStatus, bool]:
        """Start an upload, or resume the current one if it's the same file.

        Returns (status, created). Starting a different file replaces an
        unfinished upload; an install in progress can't be replaced.
        """
        if size <= 0:
            raise UploadError("the file is empty")
        if size > self.max_bytes:
            raise UploadError(
                f"the file is larger than this server accepts ({_gib(self.max_bytes)})"
            )
        self._require_available()
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()
        async with self._lock:
            meta = self._read_meta()
            if meta is not None and meta.state == "installing":
                raise UploadError("game files are being installed; wait for that to finish")
            if (
                meta is not None
                and meta.state in ("receiving", "received")
                and (meta.size, meta.fingerprint) == (size, digest)
            ):
                return self._status(meta), False
            self._discard_files()
            free = shutil.disk_usage(self.dir.parent.parent).free
            if free < size:
                raise UploadError(
                    f"not enough free space for the upload: it needs {_gib(size)}, "
                    f"and {_gib(free)} is free"
                )
            self.dir.mkdir(parents=True, exist_ok=True)
            self._archive.touch()
            meta = _Meta(secrets.token_hex(16), size, digest, "receiving", None, 0)
            self._write_meta(meta)
            return self._status(meta), True

    def _get(self, upload_id: str) -> _Meta:
        meta = self._read_meta()
        if meta is None or not secrets.compare_digest(meta.id, upload_id):
            raise UploadNotFound("no such upload")
        return meta

    async def write(self, upload_id: str, offset: int, body: AsyncIterator[bytes]) -> UploadStatus:
        """Append one chunk, which must start where the file currently ends."""
        async with self._lock:
            meta = self._get(upload_id)
            if meta.state not in ("receiving", "received"):
                raise UploadError("this upload isn't accepting data")
            current = self._offset()
            if offset != current:
                raise OffsetMismatch(current)
            limit = min(self.chunk_size, meta.size - current)
            received = 0
            # Unbuffered appends: whatever arrives is kept, so a broken connection
            # loses at most the part of a chunk that never reached the server.
            with self._archive.open("ab", buffering=0) as out:
                async for piece in body:
                    received += len(piece)
                    if received > limit:
                        raise UploadError("the chunk is larger than allowed")
                    await asyncio.to_thread(out.write, piece)
            if self._offset() == meta.size and meta.state != "received":
                meta.state, meta.message = "received", None
                self._write_meta(meta)
            elif received:
                self._write_meta(meta)  # keeps it from looking stale
            return self._status(meta)

    async def install(self, upload_id: str) -> UploadStatus:
        """Check, extract and validate the uploaded archive, then swap it in (in the background)."""
        self._require_available()
        async with self._lock:
            meta = self._get(upload_id)
            if meta.state == "installing":
                return self._status(meta)
            if meta.state != "received" or self._offset() != meta.size:
                raise UploadError("the upload isn't complete yet")
            if self.is_busy():
                raise UploadError("a seed is being patched; try again when it finishes")
            meta.state, meta.message = "installing", "Checking the archive"
            self._write_meta(meta)
            self._progress = (0, None)
            self._task = asyncio.create_task(self._install_in_background(meta))
            return self._status(meta)

    async def discard(self, upload_id: str) -> None:
        async with self._lock:
            meta = self._get(upload_id)
            if meta.state == "installing":
                raise UploadError("the install can't be stopped once it has started")
            self._discard_files()

    async def wait_for_install(self) -> None:
        """For tests and shutdown."""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ---- installing ------------------------------------------------------------------------

    async def _install_in_background(self, meta: _Meta) -> None:
        try:
            version = await asyncio.to_thread(self._install)
        except UploadError as exc:
            outcome: tuple[State, str] = ("failed", str(exc))
        except Exception:
            log.exception("installing uploaded game files failed")
            outcome = ("failed", "internal error while installing the game files")
        else:
            outcome = ("installed", f"Installed: Dread {version}.")
        finally:
            shutil.rmtree(self._staging, ignore_errors=True)
            # Installed: the archive isn't needed. Failed: it won't get better, so start again.
            self._archive.unlink(missing_ok=True)
        state, message = outcome
        meta = replace(meta, state=state, message=message)
        self._write_meta(meta)

    def _install(self) -> str:
        with open_archive(self._archive) as entries:
            planned, total = plan_extraction(entries, self.max_files, self.max_bytes)
            free = shutil.disk_usage(self.dir).free
            if free < total + SPACE_MARGIN_BYTES:
                raise UploadError(
                    f"not enough free space to extract the archive: it needs {_gib(total)}, "
                    f"and {_gib(free)} is free"
                )
            self._staging.mkdir()
            self._progress = (0, total)
            extract(planned, self._staging, self.max_bytes, self._on_progress)

        status = self.romfs.at(self._staging).status()
        if not status.ready:
            raise UploadError(status.message)
        if self.is_busy():
            raise UploadError("a seed started patching during the install; install again")
        _swap_in(self._staging, self.romfs.root)
        return status.version or "?"

    def _on_progress(self, done: int) -> None:
        self._progress = (done, self._progress[1])


_COMPRESSION_HELP = (
    "This zip uses a compression method the server can't read (Windows uses Deflate64 "
    "for large files). Make the archive with 7-Zip using standard Deflate, or upload a "
    ".tar or .tar.gz instead."
)
_DAMAGED = "the archive is damaged, or it isn't a complete zip or tar file"


@dataclass(frozen=True)
class Entry:
    """One archive member, whatever the format. Only regular files can be opened."""

    name: str
    size: int
    kind: Literal["file", "dir", "link", "special"]
    open: Callable[[], IO[bytes]]
    # Why this entry can never be extracted (encryption, compression), if anything.
    problem: str | None = None


@contextlib.contextmanager
def open_archive(path: Path) -> Iterator[list[Entry]]:
    """List a zip or tar archive (tar may be gzip, bzip2 or xz compressed).

    The format is recognised from the content, not the file name. Format errors
    while listing or reading become UploadError.
    """
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                yield [_zip_entry(archive, info) for info in archive.infolist()]
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as archive:
                yield [_tar_entry(archive, member) for member in archive.getmembers()]
        elif _starts_with(path, b"PK\x03\x04"):
            raise UploadError(_DAMAGED)  # a zip that was cut off: no central directory
        else:
            raise UploadError(
                "The file isn't a zip or tar archive. Upload a .zip, .tar, .tar.gz, .tar.bz2 "
                "or .tar.xz of the RomFS folder."
            )
    except (
        zipfile.BadZipFile,
        tarfile.TarError,
        EOFError,
        OSError,
        zlib.error,
        lzma.LZMAError,
    ) as exc:
        if isinstance(exc, OSError) and exc.errno is not None:
            raise  # a real I/O problem on the server, not a bad archive
        raise UploadError(_DAMAGED) from None
    except NotImplementedError:
        raise UploadError(_COMPRESSION_HELP) from None


def _starts_with(path: Path, magic: bytes) -> bool:
    with path.open("rb") as f:
        return f.read(len(magic)) == magic


def _zip_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> Entry:
    mode = info.external_attr >> 16
    kind: Literal["file", "dir", "link", "special"] = (
        "dir" if info.is_dir() else "link" if stat.S_ISLNK(mode) else "file"
    )
    problem = None
    if info.flag_bits & 0x1:
        problem = "the zip is encrypted; upload an unencrypted archive"
    elif info.compress_type not in SUPPORTED_COMPRESSION:
        problem = _COMPRESSION_HELP
    return Entry(
        name=info.filename.replace("\\", "/"),
        size=info.file_size,
        kind=kind,
        open=lambda: archive.open(info),
        problem=problem,
    )


def _tar_entry(archive: tarfile.TarFile, member: tarfile.TarInfo) -> Entry:
    kind: Literal["file", "dir", "link", "special"] = (
        "file" if member.isfile() else "dir" if member.isdir()
        else "link" if member.issym() or member.islnk() else "special"
    )  # fmt: skip

    def open_member() -> IO[bytes]:
        source = archive.extractfile(member)
        if source is None:
            raise UploadError(_DAMAGED)
        return source

    return Entry(name=member.name, size=member.size, kind=kind, open=open_member)


def plan_extraction(
    entries: list[Entry], max_files: int, max_bytes: int
) -> tuple[list[tuple[Entry, tuple[str, ...]]], int]:
    """Check every entry before anything is written. Returns (entries, total size).

    The RomFS may be at the top of the archive or inside one folder: its root is
    wherever ``system/files.toc`` is. Entries outside that folder are ignored.
    """
    if len(entries) > max_files * 2:
        raise UploadError("the archive has far too many entries to be a RomFS")
    tocs = [e.name for e in entries if e.name == TOC or e.name.endswith("/" + TOC)]
    if not tocs:
        if any(Path(e.name).suffix.lower() in PACKAGE_SUFFIXES for e in entries):
            raise UploadError(
                "This archive contains a game package, not an extracted RomFS. Dreadovania "
                "only accepts the extracted RomFS folder, dumped from your own game."
            )
        raise UploadError("This archive doesn't contain a Dread RomFS (no system/files.toc).")
    if len(tocs) > 1:
        raise UploadError("This archive contains more than one RomFS. Upload just one.")
    prefix = tocs[0][: -len(TOC)]

    planned: list[tuple[Entry, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()
    total = 0
    for entry in entries:
        if not entry.name.startswith(prefix) or entry.kind == "dir":
            continue
        parts = tuple(entry.name[len(prefix) :].split("/"))
        if any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
            raise UploadError("the archive contains an unsafe path")
        if entry.kind == "link":
            raise UploadError("the archive contains a link; upload plain files only")
        if entry.kind == "special":
            raise UploadError("the archive contains something other than plain files")
        if entry.problem is not None:
            raise UploadError(entry.problem)
        if parts in seen:
            raise UploadError("the archive contains the same file twice")
        seen.add(parts)
        total += entry.size
        planned.append((entry, parts))
        if len(planned) > max_files:
            raise UploadError("the archive has too many files to be a RomFS")
        if total > max_bytes:
            raise UploadError(
                f"the RomFS in the archive is larger than this server accepts ({_gib(max_bytes)})"
            )
    return planned, total


def extract(
    planned: list[tuple[Entry, tuple[str, ...]]],
    destination: Path,
    max_bytes: int,
    on_progress: Callable[[int], None],
) -> None:
    """Write the planned entries, counting real bytes: sizes in an archive can lie."""
    root = destination.resolve()
    done = 0
    for entry, parts in planned:
        path = destination.joinpath(*parts)
        if not path.resolve().is_relative_to(root):
            raise UploadError("the archive contains an unsafe path")
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with entry.open() as source, path.open("xb") as out:
            while chunk := source.read(COPY_CHUNK):
                written += len(chunk)
                done += len(chunk)
                if written > entry.size or done > max_bytes:
                    raise UploadError("the archive's contents are larger than it says")
                out.write(chunk)
                on_progress(done)


def _swap_in(staging: Path, target: Path) -> None:
    """Replace the game files with the new ones by renaming (same disk, nearly atomic)."""
    old = None
    if target.exists():
        old = target.with_name(f".{target.name}-old-{secrets.token_hex(4)}")
        os.replace(target, old)
    try:
        os.replace(staging, target)
    except OSError:
        if old is not None:
            os.replace(old, target)  # put the previous files back
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def _gib(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"
