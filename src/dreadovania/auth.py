# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign-in: setup token, passwords, sessions, rate limiting, origin checks.

Implements ADR 0004 and ADR 0011 §4. This module has no FastAPI imports; the
HTTP layer is in ``routes_auth``.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from dreadovania.db import Database, utc_now
from dreadovania.jobs import new_id

SETUP_TOKEN_LIFETIME = timedelta(hours=1)
SESSION_LIFETIME = timedelta(days=90)
# A session's expiry is pushed forward at most this often, to avoid a write per request.
SESSION_RENEW_AFTER = timedelta(hours=1)

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # Argon2 hashes any length; this only bounds the work
USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

_hasher = PasswordHasher()  # Argon2id with the library's current defaults
# Verified against when the username doesn't exist, so both paths take the same time.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


class AuthError(Exception):
    """The message is safe to show to the user."""


class SetupAlreadyDone(AuthError):
    pass


class InvalidSetupToken(AuthError):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _now() -> datetime:
    return datetime.now(UTC)


# ---- validation -------------------------------------------------------------------


def check_username(username: str) -> str:
    if not USERNAME_RE.fullmatch(username):
        raise AuthError("usernames are 1-64 letters, digits, dots, dashes or underscores")
    return username


def check_new_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"use a password of at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"passwords can be at most {MAX_PASSWORD_LENGTH} characters")
    return password


def origin_of(url: str) -> str:
    """Normalise a URL to its origin, e.g. ``https://dread.example`` (no default port)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise AuthError("the public URL must start with http:// or https://")
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    if port is None or (scheme, port) in (("http", 80), ("https", 443)):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


# ---- setup ---------------------------------------------------------------------------


def setup_required(db: Database) -> bool:
    with db.connect() as conn:
        return (
            conn.execute("SELECT 1 FROM users WHERE username IS NOT NULL LIMIT 1").fetchone()
            is None
        )


def create_setup_token(db: Database) -> str:
    """Replace any previous setup token with a new one; returns it (never stored)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with db.transaction() as conn:
        conn.execute("DELETE FROM setup_tokens")
        conn.execute(
            "INSERT INTO setup_tokens (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (_hash_token(token), _iso(now), _iso(now + SETUP_TOKEN_LIFETIME)),
        )
    return token


def complete_setup(db: Database, token: str, username: str, password: str, public_url: str) -> str:
    """Create the first account. Returns the new user's ID."""
    check_username(username)
    check_new_password(password)
    public_origin = origin_of(public_url)
    password_hash = _hasher.hash(password)
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username IS NOT NULL").fetchone():
            raise SetupAlreadyDone("setup has already been completed")
        row = conn.execute(
            "SELECT expires_at FROM setup_tokens WHERE token_hash = ?", (_hash_token(token),)
        ).fetchone()
        if row is None or row["expires_at"] <= _iso(_now()):
            raise InvalidSetupToken(
                "this setup link is invalid or has expired; restart the server for a new one"
            )
        user_id, now = new_id(), utc_now()
        conn.execute(
            "INSERT INTO users (id, created_at, username, role) VALUES (?, ?, ?, 'admin')",
            (user_id, now, username),
        )
        conn.execute(
            "INSERT INTO credentials (user_id, password_hash, updated_at) VALUES (?, ?, ?)",
            (user_id, password_hash, now),
        )
        conn.execute(
            "INSERT INTO instance_settings (key, value) VALUES ('public_url', ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (public_origin,),
        )
        conn.execute("DELETE FROM setup_tokens")  # single use
    return user_id


def stored_public_origin(db: Database) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM instance_settings WHERE key = 'public_url'"
        ).fetchone()
    return row["value"] if row else None


# ---- passwords --------------------------------------------------------------------------


def authenticate(db: Database, username: str, password: str) -> str:
    """Returns the user's ID, or raises AuthError with a deliberately generic message."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.id, c.password_hash FROM users u JOIN credentials c ON c.user_id = u.id"
            " WHERE u.username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    stored = row["password_hash"] if row else _DUMMY_HASH
    try:
        _hasher.verify(stored, password[:MAX_PASSWORD_LENGTH])
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        raise AuthError("wrong username or password") from None
    if row is None:  # the dummy hash can't match a real password, but be explicit
        raise AuthError("wrong username or password")
    if _hasher.check_needs_rehash(stored):
        with db.connect() as conn:
            conn.execute(
                "UPDATE credentials SET password_hash = ?, updated_at = ? WHERE user_id = ?",
                (_hasher.hash(password), utc_now(), row["id"]),
            )
    return str(row["id"])


# ---- sessions ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    user_id: str
    username: str
    renewed: bool  # True when the expiry was just pushed forward (re-send the cookie)
    device_id: str | None = None  # set when a paired device's token is used (ADR 0017)
    admin: bool = False  # the account created at setup (ADR 0020)


def create_session(db: Database, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (_hash_token(token), user_id, _iso(now), _iso(now), _iso(now + SESSION_LIFETIME)),
        )
    return token


def resolve_session(db: Database, token: str) -> Session | None:
    now = _now()
    token_hash = _hash_token(token)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT s.user_id, s.last_used_at, s.expires_at, u.username, u.role FROM sessions s"
            " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= _iso(now):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        renew = datetime.fromisoformat(row["last_used_at"]) <= now - SESSION_RENEW_AFTER
        if renew:
            conn.execute(
                "UPDATE sessions SET last_used_at = ?, expires_at = ? WHERE token_hash = ?",
                (_iso(now), _iso(now + SESSION_LIFETIME), token_hash),
            )
    return Session(
        user_id=row["user_id"], username=row["username"], renewed=renew,
        admin=row["role"] == "admin",
    )  # fmt: skip


def delete_session(db: Database, token: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


# ---- rate limiting -------------------------------------------------------------------------


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"too many attempts; try again in {retry_after} seconds")
        self.retry_after = retry_after


class AttemptLimiter:
    """Sliding-window limits on attempts, per client and overall (in memory).

    In memory is enough for one process; a restart clears it, which an attacker
    can't trigger from outside.
    """

    def __init__(
        self,
        per_client: int,
        overall: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_client = per_client
        self.overall = overall
        self.window = window_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._by_client: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def check(self, client: str) -> None:
        """Raise RateLimited if the client (or everyone together) is over the limit."""
        with self._lock:
            now = self.clock()
            mine = self._by_client.get(client, deque())
            self._prune(mine, now)
            self._prune(self._all, now)
            for q, limit in ((mine, self.per_client), (self._all, self.overall)):
                if len(q) >= limit:
                    raise RateLimited(max(1, int(q[0] + self.window - now) + 1))

    def record(self, client: str) -> None:
        with self._lock:
            now = self.clock()
            self._by_client.setdefault(client, deque()).append(now)
            self._all.append(now)
            if len(self._by_client) > 10_000:  # bound memory under a flood of addresses
                stale = [
                    k for k, q in self._by_client.items() if not q or q[-1] <= now - self.window
                ]
                for k in stale:
                    del self._by_client[k]
