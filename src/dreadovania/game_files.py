# SPDX-License-Identifier: GPL-3.0-or-later
"""Each account's own game files (ADR 0020 §3).

Patching a seed uses only its owner's game files, dumped from their own copy of
the game. The admin's are where they always were: the configured folder
(``DREADOVANIA_ROMFS_DIR``, which may be a mount, or ``data/romfs``). Every other
account has its own folder, ``data/accounts/<account id>/romfs``, which only it
can upload to, check or patch with. No route takes a folder or an account ID for
game files: the signed-in account (or a job's owner) picks the folder.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from dreadovania.db import Database
from dreadovania.romfs import RomfsInspector, _count_files
from dreadovania.romfs_upload import RomfsUploads


def admin_id(db: Database) -> str | None:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    return row["id"] if row else None


class GameFiles:
    def __init__(
        self,
        db: Database,
        data_dir: Path,
        admin_romfs: RomfsInspector,
        admin_uploads: RomfsUploads,
        *,
        max_bytes: int,
        max_files: int,
        chunk_size: int,
        export_running: Callable[[str], bool],
    ) -> None:
        self.db = db
        self.accounts_dir = data_dir / "accounts"
        self.admin_romfs = admin_romfs
        self.admin_uploads = admin_uploads
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.chunk_size = chunk_size
        self.export_running = export_running
        self._uploads: dict[str, RomfsUploads] = {}
        self._lock = threading.Lock()

    def _is_admin(self, owner_id: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute("SELECT role FROM users WHERE id = ?", (owner_id,)).fetchone()
        return row is not None and row["role"] == "admin"

    def folder(self, owner_id: str) -> Path:
        """Where a member's files live (their game files, and their uploads)."""
        if not owner_id or "/" in owner_id or owner_id in (".", ".."):
            raise ValueError("not an account ID")
        return self.accounts_dir / owner_id

    def inspector(self, owner_id: str) -> RomfsInspector:
        """The account's game files."""
        if self._is_admin(owner_id):
            return self.admin_romfs
        return self.admin_romfs.at(self.folder(owner_id) / "romfs")

    def uploads(self, owner_id: str) -> RomfsUploads:
        """Uploading the account's game files: one upload at a time per account."""
        if self._is_admin(owner_id):
            return self.admin_uploads
        with self._lock:
            uploads = self._uploads.get(owner_id)
            if uploads is None:
                uploads = RomfsUploads(
                    self.folder(owner_id),
                    self.inspector(owner_id),
                    max_bytes=self.max_bytes,
                    max_files=self.max_files,
                    chunk_size=self.chunk_size,
                    is_busy=lambda: self.export_running(owner_id),
                )
                uploads.recover()
                self._uploads[owner_id] = uploads
            return uploads

    def size(self, owner_id: str) -> int:
        """How much disk the account's game files use, in bytes (0 when there are none)."""
        root = self.inspector(owner_id).root
        try:
            return _count_files(root)[1] if root.is_dir() else 0
        except OSError:
            return 0

    def installing(self, owner_id: str) -> bool:
        """Whether new game files are being installed for this account right now."""
        if self._is_admin(owner_id):
            return self.admin_uploads.installing
        with self._lock:
            uploads = self._uploads.get(owner_id)
        return uploads is not None and uploads.installing

    def remove(self, owner_id: str) -> None:
        """Delete a removed member's game files and uploads."""
        with self._lock:
            self._uploads.pop(owner_id, None)
        shutil.rmtree(self.folder(owner_id), ignore_errors=True)
