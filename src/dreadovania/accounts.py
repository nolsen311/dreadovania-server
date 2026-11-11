# SPDX-License-Identifier: GPL-3.0-or-later
"""Accounts for friends on the same server: roles, invites, removal (ADR 0020).

The account created at setup is the admin. Everyone else joins with a one-time
invite link the admin creates, and is a member: they have everything the admin
has for their own data, but can't invite, list or remove accounts. This module
has no FastAPI imports; the HTTP layer is in ``routes_auth`` and ``ui``.
"""

from __future__ import annotations

import secrets
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dreadovania import auth
from dreadovania.db import Database, utc_now
from dreadovania.jobs import new_id

if TYPE_CHECKING:
    from dreadovania.game_files import GameFiles

INVITE_LIFETIME = timedelta(days=7)
MAX_NOTE = 60
MAX_LIVE_INVITES = 20


class AccountError(Exception):
    """The message is safe to show to the user."""


class InvalidInvite(AccountError):
    pass


class NotAdmin(AccountError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def is_admin(db: Database, user_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and row["role"] == "admin"


def require_admin(db: Database, user_id: str) -> None:
    if not is_admin(db, user_id):
        raise NotAdmin("only the server's admin can do this")


# ---- invites ----------------------------------------------------------------------------


def _prune(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM invites WHERE expires_at <= ?", (_iso(_now()),))


def create_invite(db: Database, admin_id: str, note: str = "") -> tuple[str, dict[str, Any]]:
    """A new one-time invite. Returns (token, invite); the token is never stored."""
    require_admin(db, admin_id)
    note = " ".join(note.split())[:MAX_NOTE]
    token = secrets.token_urlsafe(32)
    now = _now()
    invite = {"id": new_id(), "note": note, "created_at": _iso(now),
              "expires_at": _iso(now + INVITE_LIFETIME)}  # fmt: skip
    with db.transaction() as conn:
        _prune(conn)
        live = conn.execute("SELECT count(*) FROM invites").fetchone()[0]
        if live >= MAX_LIVE_INVITES:
            raise AccountError(f"there are already {live} unused invites; revoke some first")
        conn.execute(
            "INSERT INTO invites (id, token_hash, created_by, note, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (invite["id"], auth._hash_token(token), admin_id, note,
             invite["created_at"], invite["expires_at"]),
        )  # fmt: skip
    return token, invite


def list_invites(db: Database, admin_id: str) -> list[dict[str, Any]]:
    """Invites that haven't been used, revoked or expired, oldest first."""
    require_admin(db, admin_id)
    with db.transaction() as conn:
        _prune(conn)
        rows = conn.execute(
            "SELECT id, note, created_at, expires_at FROM invites ORDER BY created_at, rowid"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_invite(db: Database, admin_id: str, invite_id: str) -> bool:
    require_admin(db, admin_id)
    with db.connect() as conn:
        return conn.execute("DELETE FROM invites WHERE id = ?", (invite_id,)).rowcount > 0


def invite_is_valid(db: Database, token: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM invites WHERE token_hash = ?", (auth._hash_token(token),)
        ).fetchone()
    return row is not None and row["expires_at"] > _iso(_now())


def accept_invite(db: Database, token: str, username: str, password: str) -> str:
    """Create a member account with an invite, which is then used up. Returns its ID."""
    auth.check_username(username)
    auth.check_new_password(password)
    password_hash = auth._hasher.hash(password)
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT id, expires_at FROM invites WHERE token_hash = ?", (auth._hash_token(token),)
        ).fetchone()
        if row is None or row["expires_at"] <= _iso(_now()):
            raise InvalidInvite("this invite link is invalid, used or expired; ask for a new one")
        taken = conn.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if taken:
            raise AccountError("that username is taken; choose another")
        user_id, now = new_id(), utc_now()
        conn.execute(
            "INSERT INTO users (id, created_at, username, role) VALUES (?, ?, ?, 'member')",
            (user_id, now, username),
        )
        conn.execute(
            "INSERT INTO credentials (user_id, password_hash, updated_at) VALUES (?, ?, ?)",
            (user_id, password_hash, now),
        )
        conn.execute("DELETE FROM invites WHERE id = ?", (row["id"],))  # single use
    return user_id


# ---- accounts ---------------------------------------------------------------------------


def list_accounts(db: Database, admin_id: str) -> list[dict[str, Any]]:
    """Every account, the admin first, with how many seeds each has."""
    require_admin(db, admin_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT u.id, u.username, u.role, u.created_at,"
            " (SELECT count(*) FROM seeds s WHERE s.owner_id = u.id) AS seeds"
            " FROM users u WHERE u.username IS NOT NULL"
            " ORDER BY u.role = 'admin' DESC, u.created_at, u.rowid"
        ).fetchall()
    return [dict(r) for r in rows]


def remove_account(
    db: Database, data_dir: Path, admin_id: str, user_id: str, game_files: GameFiles | None = None
) -> str | None:
    """Delete a member and everything they own. Returns their username, or None if unknown.

    Their seeds, mods, presets, game files, Switches, passkeys, sessions and paired
    devices go with them (the database cascades; files are removed from disk).
    Refused while one of their jobs is queued or running, while their game files
    are being installed, and for the admin's own account.
    """
    require_admin(db, admin_id)
    if game_files is not None and game_files.installing(user_id):
        raise AccountError("their game files are being installed; try again when that finishes")
    with db.transaction() as conn:
        row = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or row["username"] is None:
            return None
        if row["role"] == "admin":
            raise AccountError("the admin account can't be removed")
        busy = conn.execute(
            "SELECT 1 FROM jobs WHERE owner_id = ? AND status IN ('queued', 'running') LIMIT 1",
            (user_id,),
        ).fetchone()
        if busy:
            raise AccountError(
                f"{row['username']} has a job waiting or running; try again when it finishes"
            )
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    shutil.rmtree(data_dir / "seeds" / user_id, ignore_errors=True)
    if game_files is not None:
        game_files.remove(user_id)
    return str(row["username"])
