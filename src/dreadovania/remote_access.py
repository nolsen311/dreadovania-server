# SPDX-License-Identifier: GPL-3.0-or-later
"""Secure remote access: the setting, the front door, and who checks (ADR 0023).

Off by default. The admin chooses who checks client certificates:

- ``server``: the server serves HTTPS itself (ADR 0005) and its TLS layer asks
  for a certificate from the built-in CA on the main port. The front-door paths
  are served on a second port, without a certificate.
- ``proxy``: a reverse proxy that ends TLS checks against ``ca.pem`` and
  ``crl.pem``, leaving the front-door paths open.
- ``cloudflare``: Cloudflare checks, with a WAF rule that leaves the front-door
  paths open.

Behind a proxy or Cloudflare the server can also check the certificate the proxy
passes on (``check_forwarded``), read only from ``DREADOVANIA_TRUSTED_PROXIES``.

The setting is stored in ``instance_settings``. This module has no FastAPI
imports; the HTTP layer is in ``routes_auth``, ``ui`` and the app's middleware.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from dreadovania import accounts
from dreadovania.db import Database, utc_now

if TYPE_CHECKING:
    from dreadovania.client_certs import ClientCertificates

Mode = Literal["server", "proxy", "cloudflare"]
MODES: tuple[Mode, ...] = ("server", "proxy", "cloudflare")
SETTING_KEY = "remote_access"
CACHE_SECONDS = 2.0
EXPIRY_WARNING_DAYS = 30

# The connection the request came on, put in the ASGI scope by ``connection.py``.
PEER_KEY = "dreadovania.peer"

# The front door (ADR 0023 §4, amended): what works without a certificate, or nobody
# could get one. First-run setup isn't here: secure remote access is only turned on
# after it, and setup stays closed while it's on. /healthz is, for Docker's health
# check and a proxy's: it answers "ok" and nothing else.
FRONT_DOOR_PATHS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/healthz"),
    ("HEAD", "/healthz"),
    ("POST", "/api/v1/devices/pair"),
    ("GET", "/invite"),
    ("HEAD", "/invite"),
    ("POST", "/invite"),
    ("POST", "/api/v1/invites/accept"),
})  # fmt: skip
FRONT_DOOR_PREFIXES = ("/static/",)  # the pages' stylesheet, scripts and icons

# Headers a trusted proxy may pass the client's certificate in.
SHA256_HEADER = "x-ssl-client-sha256"  # HAProxy: %[ssl_c_der,sha2(256),hex]
CLIENT_CERT_HEADER = "client-cert"  # RFC 9440, :base64 DER: (HAProxy, Cloudflare)
_HEX64 = re.compile(r"[0-9a-f]{64}")


class RemoteAccessError(Exception):
    """The message is safe to show to the admin."""


@dataclass(frozen=True)
class Peer:
    """The TCP connection a request arrived on, before any forwarded headers."""

    host: str | None
    client_cert: bytes | None  # DER, only when the TLS layer verified it
    front_door: bool = False


@dataclass(frozen=True)
class State:
    enabled: bool = False
    mode: Mode | None = None
    check_forwarded: bool = True
    changed_at: str | None = None


def is_front_door(method: str, path: str) -> bool:
    if (method.upper(), path) in FRONT_DOOR_PATHS:
        return True
    return method.upper() in ("GET", "HEAD") and path.startswith(FRONT_DOOR_PREFIXES)


def front_door_list() -> list[str]:
    """For the settings page and the docs: every path left open, sorted."""
    paths = sorted({f"{m} {p}" for m, p in FRONT_DOOR_PATHS if m != "HEAD"})
    return [*paths, *(f"GET {p}*" for p in FRONT_DOOR_PREFIXES)]


def cloudflare_expression() -> str:
    """The WAF custom rule to paste into Cloudflare (action: Block)."""
    exact = sorted({p for _m, p in FRONT_DOOR_PATHS})
    paths = " ".join(f'"{p}"' for p in exact)
    prefixes = " or ".join(
        f'starts_with(http.request.uri.path, "{p}")' for p in FRONT_DOOR_PREFIXES
    )
    return (
        "(not cf.tls_client_auth.cert_verified or cf.tls_client_auth.cert_revoked)"
        f" and not (http.request.uri.path in {{{paths}}} or {prefixes})"
    )


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def forwarded_fingerprint(headers: Mapping[str, str]) -> str | None:
    """The SHA-256 fingerprint a proxy passed on, from either header, or None."""
    value = headers.get(SHA256_HEADER)
    if value:
        hexed = value.strip().lower().replace(":", "")
        return hexed if _HEX64.fullmatch(hexed) else None
    value = headers.get(CLIENT_CERT_HEADER)
    if value:
        value = value.strip()
        if len(value) < 3 or not (value.startswith(":") and value.endswith(":")):
            return None
        try:
            der = base64.b64decode(value[1:-1], validate=True)
        except (binascii.Error, ValueError):
            return None
        return fingerprint(der) if der else None
    return None


def is_trusted(host: str | None, trusted_proxies: list[str]) -> bool:
    if not host or not trusted_proxies:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(net, strict=False) for net in trusted_proxies)


@dataclass(frozen=True)
class Refusal:
    status: int
    title: str
    detail: str


class RemoteAccess:
    """The setting, and the checks that follow from it."""

    def __init__(
        self,
        db: Database,
        certs: ClientCertificates,
        *,
        serves_tls: bool,
        trusted_proxies: list[str],
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self.db, self.certs = db, certs
        self.serves_tls, self.trusted_proxies = serves_tls, trusted_proxies
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: tuple[float, State] | None = None
        self._listeners: list[Callable[[State], None]] = []

    # ---- the setting ------------------------------------------------------------------

    def state(self) -> State:
        """The current setting, re-read every couple of seconds (the CLI may change it)."""
        now = time.monotonic()
        with self._lock:
            if self._cached and now - self._cached[0] < self.cache_seconds:
                return self._cached[1]
        state = self._read()
        with self._lock:
            self._cached = (now, state)
        return state

    def _read(self) -> State:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM instance_settings WHERE key = ?", (SETTING_KEY,)
            ).fetchone()
        if row is None:
            return State()
        try:
            data = json.loads(row["value"])
            mode = data.get("mode")
            return State(
                enabled=bool(data.get("enabled")) and mode in MODES,
                mode=mode if mode in MODES else None,
                check_forwarded=bool(data.get("check_forwarded", True)),
                changed_at=data.get("changed_at"),
            )
        except (ValueError, AttributeError):
            return State()  # unreadable: off, as by default

    def _write(self, state: State) -> State:
        state = replace(state, changed_at=utc_now())
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO instance_settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (SETTING_KEY, json.dumps(asdict(state))),
            )
        with self._lock:
            self._cached = (time.monotonic(), state)
        for listener in list(self._listeners):
            listener(state)
        return state

    def on_change(self, listener: Callable[[State], None]) -> None:
        self._listeners.append(listener)

    def choose(self, admin_id: str, mode: Mode | None, *, check_forwarded: bool = True) -> State:
        """Choose who checks, without turning it on: the first step of the settings page."""
        accounts.require_admin(self.db, admin_id)
        if mode is not None and mode not in MODES:
            raise RemoteAccessError("choose the server, a proxy or Cloudflare")
        if mode == "server" and not self.serves_tls:
            raise RemoteAccessError(
                "this server doesn't serve HTTPS itself (DREADOVANIA_TLS_CERT_FILE isn't set):"
                " choose a proxy or Cloudflare, or set up HTTPS first (docs/https.md)"
            )
        current = self.state()
        if current.enabled and mode != current.mode:
            raise RemoteAccessError("turn secure remote access off before changing who checks")
        return self._write(replace(current, mode=mode, check_forwarded=check_forwarded))

    def turn_on(self, admin_id: str, presented: dict[str, Any] | None, *, confirmed: bool) -> State:
        """Turn it on, but only once this browser is known to get through (ADR 0023 §5).

        ``presented`` is the certificate this request came with (``certificate_of``).
        When the server checks, or the proxy passes the certificate on, it must be
        one of the admin's own. When the proxy checks alone, the server can't see
        it, so the admin confirms they checked.
        """
        accounts.require_admin(self.db, admin_id)
        current = self.state()
        if current.mode is None:
            raise RemoteAccessError("choose who checks certificates first")
        if current.mode == "server" and not self.serves_tls:
            raise RemoteAccessError("this server doesn't serve HTTPS itself any more")
        if current.mode == "cloudflare":
            # Cloudflare only accepts certificates from its own CA on most plans, and
            # issuing those through its API is the next part of ADR 0023.
            raise RemoteAccessError(
                "Cloudflare checks certificates from its own CA, which this version can't"
                " issue yet: for now, choose this server or a proxy that ends TLS"
            )
        if self.server_sees_certificates(current):
            if presented is None:
                raise RemoteAccessError(
                    "this browser didn't present a certificate from this server: add one on"
                    " your Account page, install it, then reload this page and try again"
                )
            if presented["owner_id"] != admin_id:
                raise RemoteAccessError("this browser's certificate isn't one of yours")
        elif not confirmed:
            raise RemoteAccessError(
                "confirm that you've set up the proxy and checked that this browser connects"
                " with its certificate"
            )
        return self._write(replace(current, enabled=True))

    def turn_off(self, admin_id: str | None) -> State:
        """Turn it off. ``admin_id`` None is the command line, for a locked-out owner."""
        if admin_id is not None:
            accounts.require_admin(self.db, admin_id)
        return self._write(replace(self.state(), enabled=False))

    # ---- checks ------------------------------------------------------------------------

    @staticmethod
    def server_sees_certificates(state: State) -> bool:
        return state.mode == "server" or (
            state.mode in ("proxy", "cloudflare") and state.check_forwarded
        )

    def verify_mode(self) -> ssl.VerifyMode:
        """What the main port's TLS layer asks of clients, when the server serves HTTPS.

        Required when the server checks and it's on; optional while the server has
        been chosen but it's still off (so the admin can check their browser's
        certificate first); otherwise not asked for, as before this feature.
        """
        state = self.state()
        if state.mode != "server" or not self.certs.ca_cert_path.is_file():
            return ssl.CERT_NONE
        return ssl.CERT_REQUIRED if state.enabled else ssl.CERT_OPTIONAL

    def certificate_of(
        self, peer: Peer | None, headers: Mapping[str, str], state: State | None = None
    ) -> dict[str, Any] | None:
        """The valid certificate this request came with, from the TLS layer or the proxy."""
        state = state or self.state()
        sha256: str | None = None
        if peer is not None and peer.client_cert:
            sha256 = fingerprint(peer.client_cert)
        elif (
            state.mode in ("proxy", "cloudflare")
            and peer is not None
            and is_trusted(peer.host, self.trusted_proxies)
        ):
            sha256 = forwarded_fingerprint(headers)
        if sha256 is None or not self.certs.is_valid(sha256):
            return None
        return self.certs.by_fingerprint(sha256)

    def check(
        self, method: str, path: str, peer: Peer | None, headers: Mapping[str, str]
    ) -> Refusal | None:
        """Whether a request may go on. None lets it through."""
        if peer is not None and peer.front_door:
            if is_front_door(method, path):
                return None
            return Refusal(404, "Not found", "this port only serves pairing and invites")
        state = self.state()
        if not state.enabled or is_front_door(method, path):
            return None
        if not self.server_sees_certificates(state):
            return None  # the proxy or Cloudflare checks, and the admin confirmed it does
        if self.certificate_of(peer, headers, state) is not None:
            return None
        return Refusal(
            403,
            "Certificate required",
            "this server only answers devices with a certificate from it: install yours,"
            " or ask the server's owner for one",
        )

    def status(self, peer: Peer | None, headers: Mapping[str, str]) -> dict[str, Any]:
        """For the settings page and the API."""
        state = self.state()
        presented = self.certificate_of(peer, headers, state)
        via_proxy = peer is not None and is_trusted(peer.host, self.trusted_proxies)
        return {
            "enabled": state.enabled,
            "mode": state.mode,
            "check_forwarded": state.check_forwarded,
            "changed_at": state.changed_at,
            "serves_tls": self.serves_tls,
            "trusted_proxies": bool(self.trusted_proxies),
            "via_trusted_proxy": via_proxy,
            "forwarded_header": via_proxy and forwarded_fingerprint(headers) is not None,
            "presented": presented,
            "front_door": front_door_list(),
            "cloudflare_expression": cloudflare_expression(),
        }


def expiring_soon(cert: dict[str, Any] | None, *, days: int = EXPIRY_WARNING_DAYS) -> int | None:
    """Days until a browser certificate expires, when that's within ``days``; else None."""
    if not cert:
        return None
    left = datetime.fromisoformat(cert["not_after"]) - datetime.now(UTC)
    whole = max(0, left.days)
    return whole if left.total_seconds() <= days * 86400 else None
