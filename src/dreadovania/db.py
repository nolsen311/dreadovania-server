# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite storage: connections and forward-only migrations (ADR 0011 §1)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

_MIGRATION_NAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")


class MigrationError(RuntimeError):
    pass


def utc_now() -> str:
    """Timestamps are stored as UTC ISO 8601 strings, which sort correctly as text."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _bundled_migrations() -> list[tuple[int, str, str]]:
    found = []
    for entry in files("dreadovania.migrations").iterdir():
        match = _MIGRATION_NAME.fullmatch(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


class Database:
    """Opens a short-lived connection per unit of work.

    SQLite connections aren't shared between threads; the API runs handlers in
    a thread pool and the job worker in the event loop, so each caller gets its
    own connection. Opening one is cheap.
    """

    def __init__(self, path: Path, migrations: list[tuple[int, str, str]] | None = None):
        self.path = path
        self._migrations = migrations if migrations is not None else _bundled_migrations()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection in autocommit mode, for single statements and reads."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction, taken immediately so concurrent writers queue up."""
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @property
    def latest_version(self) -> int:
        return self._migrations[-1][0] if self._migrations else 0

    def schema_version(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """Apply every migration newer than the database. Returns the new version."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current > self.latest_version:
                raise MigrationError(
                    f"database schema version {current} is newer than this server "
                    f"({self.latest_version}); refusing to start rather than risk data loss"
                )
            for version, name, sql in self._migrations:
                if version <= current:
                    continue
                try:
                    # executescript can't run inside an open transaction, so the
                    # transaction is part of the script; any error rolls it back.
                    conn.executescript(
                        f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except sqlite3.Error as exc:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise MigrationError(f"migration {name} failed: {exc}") from exc
                current = version
        return current


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
