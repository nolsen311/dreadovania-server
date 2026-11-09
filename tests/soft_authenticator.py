# SPDX-License-Identifier: GPL-3.0-or-later
"""A software passkey authenticator for tests: it does what a phone or password
manager does, with a real P-256 key, so py_webauthn's checks run for real."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

UP, UV, AT = 0x01, 0x04, 0x40  # user present, user verified, attested credential data


class SoftAuthenticator:
    def __init__(self, origin: str, *, verify_user: bool = True) -> None:
        self.origin = origin
        self.verify_user = verify_user
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.user_handle: bytes | None = None
        self.count = 0

    @property
    def id(self) -> str:
        return bytes_to_base64url(self.credential_id)

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin or self.origin}
        ).encode()

    def _flags(self, extra: int = 0) -> int:
        return UP | (UV if self.verify_user else 0) | extra

    def create(self, options: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        """navigator.credentials.create() with the server's options."""
        rp_id = options["rp"]["id"]
        self.user_handle = base64url_to_bytes(options["user"]["id"])
        numbers = self.key.public_key().public_numbers()
        cose_key = cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
             -3: numbers.y.to_bytes(32, "big")}
        )  # fmt: skip
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([self._flags(AT)])
            + struct.pack(">I", self.count)
            + bytes(16)  # AAGUID
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + cose_key
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def get(
        self, options: dict[str, Any], *, origin: str | None = None, rp_id: str | None = None
    ) -> dict[str, Any]:
        """navigator.credentials.get() with the server's options."""
        self.count += 1
        auth_data = (
            hashlib.sha256((rp_id or options["rpId"]).encode()).digest()
            + bytes([self._flags()])
            + struct.pack(">I", self.count)
        )
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(self.user_handle or b""),
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }
