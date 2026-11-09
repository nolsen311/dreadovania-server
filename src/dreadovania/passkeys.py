# SPDX-License-Identifier: GPL-3.0-or-later
"""Passkeys (WebAuthn) for signing in (ADR 0004).

The signature and attestation checks are done by py_webauthn. This module
decides what is allowed:

- Passkeys only work over HTTPS at a hostname, which is the "relying party ID"
  they are bound to. An IP address or plain HTTP (other than localhost, which
  browsers treat as secure) can't use them.
- Every ceremony uses a one-time, server-generated challenge that expires after
  a few minutes, so a captured response can't be replayed.
- A passkey is tied to the hostname it was made for; if the server's hostname
  changes, old passkeys stop matching and password sign-in remains.
"""

from __future__ import annotations

import ipaddress
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidCBORData,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from dreadovania.db import Database, utc_now

RP_NAME = "Dreadovania"
CHALLENGE_LIFETIME = timedelta(minutes=5)
MAX_NAME = 60
MAX_PASSKEYS = 20
MAX_RESPONSE_BYTES = 64 * 1024

# What a passkey must be: discoverable (so sign-in needs no username), and
# verified by the device (PIN, fingerprint or face) when used.
_SELECTION = AuthenticatorSelectionCriteria(
    resident_key=ResidentKeyRequirement.REQUIRED,
    user_verification=UserVerificationRequirement.REQUIRED,
)


class PasskeyError(Exception):
    """The passkey couldn't be used. The message is safe to show to the user."""


@dataclass(frozen=True)
class Party:
    """Where passkeys live: the server's public origin and its hostname."""

    origin: str
    rp_id: str


def party_for(public_origin: str | None) -> Party:
    """The relying party for this server, or why passkeys can't be used here."""
    if public_origin is None:
        raise PasskeyError("finish setup first")
    parts = urlsplit(public_origin)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PasskeyError(
            "passkeys need the server to be at a hostname, not an IP address: see docs/https.md"
        )
    if parts.scheme != "https" and host != "localhost":
        raise PasskeyError("passkeys need HTTPS: see docs/https.md")
    return Party(origin=public_origin, rp_id=host)


def _now() -> datetime:
    return datetime.now(UTC)


def _store_challenge(db: Database, purpose: str, user_id: str | None, challenge: bytes) -> str:
    challenge_id = str(uuid.uuid4())
    now = _now()
    with db.connect() as conn:
        conn.execute("DELETE FROM webauthn_challenges WHERE expires_at <= ?", (now.isoformat(),))
        conn.execute(
            "INSERT INTO webauthn_challenges (id, purpose, user_id, challenge, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (challenge_id, purpose, user_id, challenge,
             (now + CHALLENGE_LIFETIME).isoformat()),
        )  # fmt: skip
    return challenge_id


def _take_challenge(db: Database, challenge_id: str, purpose: str, user_id: str | None) -> bytes:
    """Use a challenge once. Expired, reused or someone else's: refused."""
    with db.transaction() as conn:
        row = conn.execute(
            "DELETE FROM webauthn_challenges WHERE id = ? AND purpose = ?"
            " RETURNING user_id, challenge, expires_at",
            (challenge_id, purpose),
        ).fetchone()
    if row is None or row["expires_at"] <= _now().isoformat() or row["user_id"] != user_id:
        raise PasskeyError("the passkey request expired: try again")
    return bytes(row["challenge"])


def _parse(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or len(json.dumps(response)) > MAX_RESPONSE_BYTES:
        raise PasskeyError("the browser's passkey response isn't valid")
    return response


# ---- adding a passkey ----------------------------------------------------------------------


def registration_options(
    db: Database, party: Party, user_id: str, username: str
) -> tuple[str, dict[str, Any]]:
    """Options for navigator.credentials.create(), and the challenge's ID."""
    with db.connect() as conn:
        existing = [
            r["id"] for r in conn.execute("SELECT id FROM passkeys WHERE user_id = ?", (user_id,))
        ]
    if len(existing) >= MAX_PASSKEYS:
        raise PasskeyError(f"you already have {MAX_PASSKEYS} passkeys: remove one first")
    options = generate_registration_options(
        rp_id=party.rp_id,
        rp_name=RP_NAME,
        user_id=user_id.encode(),
        user_name=username,
        user_display_name=username,
        authenticator_selection=_SELECTION,
        # Don't make a second passkey on a device that already has one.
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(i)) for i in existing
        ],
    )
    challenge_id = _store_challenge(db, "register", user_id, options.challenge)
    return challenge_id, json.loads(options_to_json(options))


def register(
    db: Database, party: Party, user_id: str, challenge_id: str, response: Any, name: str
) -> dict[str, Any]:
    name = name.strip()[:MAX_NAME] or "Passkey"
    challenge = _take_challenge(db, challenge_id, "register", user_id)
    try:
        verified = verify_registration_response(
            credential=_parse(response),
            expected_challenge=challenge,
            expected_rp_id=party.rp_id,
            expected_origin=party.origin,
            require_user_verification=True,
        )
    except (InvalidRegistrationResponse, InvalidJSONStructure, InvalidCBORData) as exc:
        raise PasskeyError(f"the passkey couldn't be added ({exc})") from None
    passkey_id = bytes_to_base64url(verified.credential_id)
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM passkeys WHERE id = ?", (passkey_id,)).fetchone():
            raise PasskeyError("that passkey is already saved")
        conn.execute(
            "INSERT INTO passkeys (id, user_id, name, public_key, sign_count, rp_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (passkey_id, user_id, name, verified.credential_public_key, verified.sign_count,
             party.rp_id, utc_now()),
        )  # fmt: skip
    return {"id": passkey_id, "name": name}


# ---- signing in ----------------------------------------------------------------------------


def sign_in_options(db: Database, party: Party) -> tuple[str, dict[str, Any]]:
    """Options for navigator.credentials.get(): any of this server's passkeys."""
    options = generate_authentication_options(
        rp_id=party.rp_id, user_verification=UserVerificationRequirement.REQUIRED
    )
    challenge_id = _store_challenge(db, "sign_in", None, options.challenge)
    return challenge_id, json.loads(options_to_json(options))


def sign_in(db: Database, party: Party, challenge_id: str, response: Any) -> str:
    """Check a passkey response; returns the user's ID."""
    challenge = _take_challenge(db, challenge_id, "sign_in", None)
    credential = _parse(response)
    passkey_id = credential.get("id")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_id, public_key, sign_count FROM passkeys WHERE id = ? AND rp_id = ?",
            (passkey_id if isinstance(passkey_id, str) else "", party.rp_id),
        ).fetchone()
    if row is None:
        raise PasskeyError(
            "this passkey isn't saved on this server: sign in with your password, then add it"
        )
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=party.rp_id,
            expected_origin=party.origin,
            credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=row["sign_count"],
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, InvalidJSONStructure, InvalidCBORData) as exc:
        raise PasskeyError(f"the passkey wasn't accepted ({exc})") from None
    with db.connect() as conn:
        conn.execute(
            "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE id = ?",
            (verified.new_sign_count, utc_now(), passkey_id),
        )
    return str(row["user_id"])


# ---- managing passkeys ---------------------------------------------------------------------


def list_passkeys(db: Database, user_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, rp_id, created_at, last_used_at FROM passkeys WHERE user_id = ?"
            " ORDER BY created_at, rowid",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def remove(db: Database, user_id: str, passkey_id: str) -> bool:
    with db.connect() as conn:
        cursor = conn.execute(
            "DELETE FROM passkeys WHERE id = ? AND user_id = ?", (passkey_id, user_id)
        )
    return cursor.rowcount > 0
