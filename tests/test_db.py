# SPDX-License-Identifier: GPL-3.0-or-later
import sqlite3
from pathlib import Path

import pytest

from dreadovania.db import Database, MigrationError, utc_now


def test_migrate_creates_tables_and_records_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "dreadovania.db")
    assert db.migrate() == db.latest_version >= 1
    assert db.schema_version() == db.latest_version
    with db.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users", "seeds", "jobs"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_is_idempotent(db: Database) -> None:
    version = db.schema_version()
    assert db.migrate() == version


def test_newer_database_is_refused(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(f"PRAGMA user_version = {db.latest_version + 1}")
    with pytest.raises(MigrationError, match="newer than this server"):
        db.migrate()


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    db = Database(
        tmp_path / "x.db",
        migrations=[
            (1, "0001_ok.sql", "CREATE TABLE a (id TEXT) STRICT;"),
            (2, "0002_bad.sql", "CREATE TABLE b (id TEXT) STRICT; THIS IS NOT SQL;"),
        ],
    )
    with pytest.raises(MigrationError, match=r"0002_bad\.sql"):
        db.migrate()
    assert db.schema_version() == 1
    with db.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables
    assert "b" not in tables  # the failed migration left nothing behind


def test_foreign_keys_and_checks_are_enforced(db: Database) -> None:
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO seeds (id, owner_id, status, source, preset_key, randovania_version,"
            " created_at) VALUES ('s', 'no-such-user', 'pending', 'preset', 'p', 'v', ?)",
            (utc_now(),),
        )
    with db.connect() as conn:
        conn.execute("INSERT INTO users (id, created_at) VALUES ('u', ?)", (utc_now(),))
        with pytest.raises(sqlite3.IntegrityError):  # preset source without a preset key
            conn.execute(
                "INSERT INTO seeds (id, owner_id, status, source, randovania_version, created_at)"
                " VALUES ('s', 'u', 'pending', 'preset', 'v', ?)",
                (utc_now(),),
            )


def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute("INSERT INTO users (id, created_at) VALUES ('u', ?)", (utc_now(),))
        raise RuntimeError("boom")
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
