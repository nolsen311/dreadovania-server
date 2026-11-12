# SPDX-License-Identifier: GPL-3.0-or-later
"""Pairing the Switch app with the server (ADR 0004, ADR 0017).

1. Signed in to the web app, the user asks for a pairing code: 8 characters,
   valid for 5 minutes, usable once.
2. The Switch app sends the code (and the console's name) and gets back a
   long-lived device token, which it keeps on the SD card.
3. The app sends the token as ``Authorization: Bearer …``. A token only
   reaches the routes in ``DEVICE_ROUTES``; it can never change account
   settings, Switches, game files or other devices.

Only hashes of codes and tokens are stored. Revoking a device deletes it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from dreadovania.db import Database

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: easy to read and type
CODE_LENGTH = 8  # 32^8 ≈ 1.1e12, with attempts rate-limited
CODE_LIFETIME = timedelta(minutes=5)
TOKEN_PREFIX = "dvt_"  # noqa: S105 (a marker, not a secret)
MAX_NAME = 60
MAX_DEVICES = 20
LAST_USED_EVERY = timedelta(minutes=1)  # don't write on every request

# What a device token may do: list and generate seeds, read presets and jobs,
# patch a seed, read its result and download it. (method, route path as declared)
DEVICE_ROUTES = frozenset({
    ("GET", "/api/v1/devices/me"),
    ("DELETE", "/api/v1/devices/me"),
    ("POST", "/api/v1/devices/me/certificate"),
    ("GET", "/api/v1/presets"),
    ("GET", "/api/v1/presets/{preset_id}"),
    ("GET", "/api/v1/seeds"),
    ("POST", "/api/v1/seeds"),
    ("GET", "/api/v1/seeds/{seed_id}"),
    ("GET", "/api/v1/seeds/{seed_id}/spoiler"),
    ("POST", "/api/v1/seeds/{seed_id}/mod"),
    ("GET", "/api/v1/seeds/{seed_id}/mod/manifest"),
    ("GET", "/api/v1/seeds/{seed_id}/mod.tar"),
    ("GET", "/api/v1/jobs/{job_id}"),
    ("GET", "/api/v1/app/latest"),
    ("GET", "/api/v1/app/dreadovania.nro"),
})  # fmt: skip


class DeviceError(Exception):
    """A pairing problem. The message is safe to show to the user."""


@dataclass(frozen=True)
class Device:
    id: str
    owner_id: str
    username: str
    name: str


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def new_pairing_code(db: Database, owner_id: str) -> tuple[str, datetime]:
    """A fresh code for this user, replacing any earlier one."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    expires = _now() + CODE_LIFETIME
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO pairing_codes (owner_id, code_hash, expires_at) VALUES (?, ?, ?)"
            " ON CONFLICT (owner_id) DO UPDATE SET code_hash = excluded.code_hash,"
            " expires_at = excluded.expires_at",
            (owner_id, _hash(code), expires.isoformat()),
        )
    return format_code(code), expires


def pair(db: Database, code: str, name: str, ip: str | None) -> tuple[Device, str]:
    """Exchange a code for a device token. Returns the device and its token (shown once)."""
    normalized = normalize_code(code)
    if len(normalized) != CODE_LENGTH or any(c not in CODE_ALPHABET for c in normalized):
        raise DeviceError("that isn't a pairing code: it's 8 letters and numbers, like ABCD-EFGH")
    name = " ".join(name.split())[:MAX_NAME] or "Switch"
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    device_id, now = str(uuid.uuid4()), _now().isoformat()
    with db.transaction() as conn:
        row = conn.execute(
            "DELETE FROM pairing_codes WHERE code_hash = ? RETURNING owner_id, expires_at",
            (_hash(normalized),),
        ).fetchone()
        if row is None or row["expires_at"] <= now:
            raise DeviceError(
                "that code is wrong or has expired: get a new one on the server's Account page"
            )
        owner = row["owner_id"]
        count = conn.execute(
            "SELECT count(*) FROM devices WHERE owner_id = ?", (owner,)
        ).fetchone()[0]
        if count >= MAX_DEVICES:
            raise DeviceError(f"you already have {MAX_DEVICES} paired devices: remove one first")
        conn.execute(
            "INSERT INTO devices (id, owner_id, name, token_hash, created_at, last_used_at,"
            " last_ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device_id, owner, name, _hash(token), now, now, ip),
        )
        username = conn.execute("SELECT username FROM users WHERE id = ?", (owner,)).fetchone()[0]
    return Device(device_id, owner, username, name), token


def resolve_token(db: Database, token: str, ip: str | None) -> Device | None:
    """The device a bearer token belongs to, recording when and where it was last used."""
    if not token.startswith(TOKEN_PREFIX) or len(token) > 128:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT d.id, d.owner_id, d.name, d.last_used_at, d.last_ip, u.username"
            " FROM devices d JOIN users u ON u.id = d.owner_id WHERE d.token_hash = ?",
            (_hash(token),),
        ).fetchone()
        if row is None:
            return None
        now = _now()
        last = datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None
        if last is None or now - last >= LAST_USED_EVERY or row["last_ip"] != ip:
            conn.execute(
                "UPDATE devices SET last_used_at = ?, last_ip = ? WHERE id = ?",
                (now.isoformat(), ip, row["id"]),
            )
    return Device(row["id"], row["owner_id"], row["username"], row["name"])


def list_devices(db: Database, owner_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, last_used_at, last_ip FROM devices"
            " WHERE owner_id = ? ORDER BY created_at, rowid",
            (owner_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_device(db: Database, owner_id: str, device_id: str) -> dict[str, Any] | None:
    return next((d for d in list_devices(db, owner_id) if d["id"] == device_id), None)


def remove(db: Database, owner_id: str, device_id: str) -> bool:
    with db.connect() as conn:
        cursor = conn.execute(
            "DELETE FROM devices WHERE id = ? AND owner_id = ?", (device_id, owner_id)
        )
    return cursor.rowcount > 0
