# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign-in logic, without HTTP (ADR 0011 §4)."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from dreadovania import auth
from dreadovania.db import Database


def _setup(db: Database, username: str = "samus", password: str = "correct horse battery") -> str:
    return auth.complete_setup(
        db, auth.create_setup_token(db), username, password, "https://dread.example"
    )


def _shift(db: Database, table: str, column: str, delta: timedelta) -> None:
    """Move stored timestamps, to test expiry without waiting."""
    with db.connect() as conn:
        for key, value in conn.execute(f"SELECT rowid, {column} FROM {table}").fetchall():  # noqa: S608
            moved = (datetime.fromisoformat(value) + delta).isoformat(timespec="milliseconds")
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (moved, key))  # noqa: S608


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Dread.Example/", "https://dread.example"),
        ("https://dread.example:443/setup", "https://dread.example"),
        ("http://dread.example:80", "http://dread.example"),
        ("http://192.168.1.5:8080/x?y#z", "http://192.168.1.5:8080"),
        ("https://[::1]:8443", "https://[::1]:8443"),
    ],
)
def test_origin_normalisation(url: str, origin: str) -> None:
    assert auth.origin_of(url) == origin


@pytest.mark.parametrize(
    "url", ["ftp://x", "dread.example", "null", "https://", "javascript:alert(1)"]
)
def test_origin_rejects_non_web_urls(url: str) -> None:
    with pytest.raises(auth.AuthError):
        auth.origin_of(url)


def test_setup_creates_account_and_stores_only_hashes(db: Database) -> None:
    token = auth.create_setup_token(db)
    assert auth.setup_required(db)
    user_id = auth.complete_setup(
        db, token, "samus", "correct horse battery", "https://dread.example/"
    )
    assert not auth.setup_required(db)
    assert auth.stored_public_origin(db) == "https://dread.example"
    with db.connect() as conn:
        stored = conn.execute("SELECT password_hash FROM credentials").fetchone()[0]
        assert stored.startswith("$argon2id$")
        assert "correct horse battery" not in stored
        assert conn.execute("SELECT count(*) FROM setup_tokens").fetchone()[0] == 0  # single use
    assert auth.authenticate(db, "SAMUS", "correct horse battery") == user_id  # case-insensitive


def test_setup_token_is_stored_hashed(db: Database) -> None:
    token = auth.create_setup_token(db)
    with db.connect() as conn:
        (stored,) = conn.execute("SELECT token_hash FROM setup_tokens").fetchone()
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert token not in stored


def test_new_setup_token_replaces_the_old_one(db: Database) -> None:
    old = auth.create_setup_token(db)
    auth.create_setup_token(db)
    with pytest.raises(auth.InvalidSetupToken):
        auth.complete_setup(db, old, "samus", "correct horse battery", "https://dread.example")


def test_expired_setup_token_is_refused(db: Database) -> None:
    token = auth.create_setup_token(db)
    _shift(db, "setup_tokens", "expires_at", -auth.SETUP_TOKEN_LIFETIME)
    with pytest.raises(auth.InvalidSetupToken):
        auth.complete_setup(db, token, "samus", "correct horse battery", "https://dread.example")


def test_setup_only_happens_once(db: Database) -> None:
    _setup(db)
    with pytest.raises(auth.SetupAlreadyDone):
        _setup(db, username="ridley")


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("samus", "short", "at least 12"),
        ("samus", "x" * (auth.MAX_PASSWORD_LENGTH + 1), "at most"),
        ("", "correct horse battery", "usernames"),
        ("has space", "correct horse battery", "usernames"),
        ("x" * 65, "correct horse battery", "usernames"),
    ],
)
def test_setup_validates_input(db: Database, username: str, password: str, message: str) -> None:
    with pytest.raises(auth.AuthError, match=message):
        _setup(db, username=username, password=password)
    assert auth.setup_required(db)  # nothing was half-created


def test_authenticate_failures_are_indistinguishable(db: Database) -> None:
    _setup(db)
    with pytest.raises(auth.AuthError) as wrong_password:
        auth.authenticate(db, "samus", "wrong password!!")
    with pytest.raises(auth.AuthError) as no_such_user:
        auth.authenticate(db, "kraid", "wrong password!!")
    assert str(wrong_password.value) == str(no_such_user.value) == "wrong username or password"


def test_sessions_are_stored_hashed_and_resolve(db: Database) -> None:
    user_id = _setup(db)
    token = auth.create_session(db, user_id)
    with db.connect() as conn:
        (stored,) = conn.execute("SELECT token_hash FROM sessions").fetchone()
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    session = auth.resolve_session(db, token)
    assert session is not None
    assert (session.user_id, session.username, session.renewed) == (user_id, "samus", False)
    assert auth.resolve_session(db, token + "x") is None


def test_expired_session_is_refused_and_removed(db: Database) -> None:
    token = auth.create_session(db, _setup(db))
    _shift(db, "sessions", "expires_at", -auth.SESSION_LIFETIME - timedelta(seconds=1))
    assert auth.resolve_session(db, token) is None
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_session_is_renewed_when_used(db: Database) -> None:
    token = auth.create_session(db, _setup(db))
    _shift(db, "sessions", "last_used_at", -auth.SESSION_RENEW_AFTER)
    _shift(db, "sessions", "expires_at", -timedelta(days=30))
    session = auth.resolve_session(db, token)
    assert session is not None and session.renewed
    with db.connect() as conn:
        (expires,) = conn.execute("SELECT expires_at FROM sessions").fetchone()
    assert datetime.fromisoformat(expires) > datetime.now(UTC) + timedelta(days=89)


def test_logout_deletes_the_session(db: Database) -> None:
    token = auth.create_session(db, _setup(db))
    auth.delete_session(db, token)
    assert auth.resolve_session(db, token) is None


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_per_client_and_window() -> None:
    clock = FakeClock()
    limiter = auth.AttemptLimiter(per_client=3, overall=100, window_seconds=60, clock=clock)
    for _ in range(3):
        limiter.check("a")
        limiter.record("a")
    with pytest.raises(auth.RateLimited) as limited:
        limiter.check("a")
    assert 1 <= limited.value.retry_after <= 61
    limiter.check("b")  # other clients are unaffected
    clock.now += 61
    limiter.check("a")  # the window has passed


def test_limiter_overall_limit_stops_distributed_guessing() -> None:
    limiter = auth.AttemptLimiter(per_client=100, overall=5, window_seconds=60, clock=FakeClock())
    for i in range(5):
        limiter.record(f"client-{i}")
    with pytest.raises(auth.RateLimited):
        limiter.check("a-new-client")
