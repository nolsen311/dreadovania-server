# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved Switches and their FTP connection test (ADR 0016, Phase 3 PR 1).

A saved Switch is where "Send to Switch" uploads a patched seed: mtheall's ftpd
on the console, on the local network. The server only ever connects to local
addresses (private, loopback, link-local or shared), checked both when a Switch
is saved and on every connection, against the addresses the name resolves to at
that moment. The FTP password is never returned or logged.
"""

from __future__ import annotations

import ftplib
import ipaddress
import re
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dreadovania.db import Database, row_to_dict, utc_now

DEFAULT_PORT = 5000  # ftpd's default
CONNECT_TIMEOUT = 8.0
MAX_NAME = 60
MAX_HOST = 253
MAX_CREDENTIAL = 128
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)

# Columns the API may see: never the password itself.
_PUBLIC = (
    "id, name, host, port, username, password IS NOT NULL AS has_password,"
    " last_test_at, last_test_ok, last_test_message, created_at, updated_at"
)

Resolver = Callable[[str, int], list[str]]


class SwitchError(Exception):
    """A user-facing problem with a Switch's settings or connection."""


@dataclass(frozen=True)
class TestResult:
    ok: bool
    message: str


def resolve(host: str, port: int) -> list[str]:
    """Every address the name resolves to (an IP literal resolves to itself)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def check_host_syntax(host: str) -> str:
    host = host.strip()
    if not host:
        raise SwitchError("enter the Switch's address, as shown in ftpd")
    if len(host) > MAX_HOST:
        raise SwitchError("the address is too long")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME.match(host):
            raise SwitchError(
                "enter just the address, like 192.168.1.50 or switch.lan: no http://, port or path"
            ) from None
    return host


def lan_address(host: str, port: int, resolver: Resolver = resolve) -> str:
    """The address to connect to, refusing anything that isn't on the local network.

    Every address the name resolves to must be local, so a name can't be used to
    reach the internet through the server.
    """
    addresses = resolver(host, port)
    if not addresses:
        raise SwitchError(f"couldn't find {host} on the network: check the address")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone
        if ip.is_global or ip.is_multicast or ip.is_unspecified:
            raise SwitchError(
                f"{host} isn't a local-network address: Dreadovania only sends to "
                "Switches on your own network"
            )
    return addresses[0]


def _clean_text(value: Any, what: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SwitchError(f"the {what} must be text")
    if len(value) > maximum or any(c in value for c in "\r\n\0"):
        raise SwitchError(f"the {what} is too long or has invalid characters")
    return value


def clean_fields(fields: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Validate the fields the user can set. ``partial`` allows any subset."""
    out: dict[str, Any] = {}
    if "name" in fields or not partial:
        name = (_clean_text(fields.get("name"), "name", MAX_NAME) or "").strip()
        if not name:
            raise SwitchError("give the Switch a name")
        out["name"] = name
    if "host" in fields or not partial:
        out["host"] = check_host_syntax(_clean_text(fields.get("host"), "address", MAX_HOST) or "")
    if "port" in fields or not partial:
        port = fields.get("port", DEFAULT_PORT)
        if port is None:
            port = DEFAULT_PORT
        if isinstance(port, str) and port.strip().isdigit():
            port = int(port.strip())
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise SwitchError("the port must be a number from 1 to 65535 (ftpd uses 5000)")
        out["port"] = port
    for key in ("username", "password"):
        if key in fields:
            value = _clean_text(fields[key], key, MAX_CREDENTIAL)
            out[key] = value if value else None  # empty means none
    return out


class SwitchStore:
    """Owner-scoped saved Switches. Another owner's Switch reads as not found."""

    def __init__(self, db: Database, resolver: Resolver = resolve) -> None:
        self.db = db
        self.resolver = resolver

    def list(self, owner_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_PUBLIC} FROM switches WHERE owner_id = ? ORDER BY created_at, rowid",  # noqa: S608
                (owner_id,),
            ).fetchall()
        return [_public(r) for r in rows]

    def get(self, owner_id: str, switch_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT {_PUBLIC} FROM switches WHERE id = ? AND owner_id = ?",  # noqa: S608
                (switch_id, owner_id),
            ).fetchone()
        return _public(row) if row else None

    def create(self, owner_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        values = clean_fields(fields)
        lan_address(values["host"], values["port"], self.resolver)
        switch_id, now = str(uuid.uuid4()), utc_now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO switches (id, owner_id, name, host, port, username, password,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (switch_id, owner_id, values["name"], values["host"], values["port"],
                 values.get("username"), values.get("password"), now, now),
            )  # fmt: skip
        created = self.get(owner_id, switch_id)
        if created is None:  # pragma: no cover - just inserted
            raise SwitchError("the Switch couldn't be saved")
        return created

    def update(
        self, owner_id: str, switch_id: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Change some fields. A password left out is kept; an empty one is removed."""
        current = self.get(owner_id, switch_id)
        if current is None:
            return None
        values = clean_fields(fields, partial=True)
        if "host" in values or "port" in values:
            lan_address(values.get("host", current["host"]), values.get("port", current["port"]),
                        self.resolver)  # fmt: skip
        if values:
            assignments = ", ".join(f"{key} = ?" for key in values)
            with self.db.connect() as conn:
                conn.execute(
                    f"UPDATE switches SET {assignments}, updated_at = ?,"  # noqa: S608 (fixed column names)
                    " last_test_at = NULL, last_test_ok = NULL, last_test_message = NULL"
                    " WHERE id = ? AND owner_id = ?",
                    (*values.values(), utc_now(), switch_id, owner_id),
                )
        return self.get(owner_id, switch_id)

    def delete(self, owner_id: str, switch_id: str) -> bool:
        with self.db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM switches WHERE id = ? AND owner_id = ?", (switch_id, owner_id)
            )
        return cursor.rowcount > 0

    def credentials(self, owner_id: str, switch_id: str) -> dict[str, Any] | None:
        """Everything needed to connect, including the password: for the server's own use."""
        with self.db.connect() as conn:
            return row_to_dict(
                conn.execute(
                    "SELECT host, port, username, password FROM switches"
                    " WHERE id = ? AND owner_id = ?",
                    (switch_id, owner_id),
                ).fetchone()
            )

    def test(
        self, owner_id: str, switch_id: str, ftp_factory: Callable[..., ftplib.FTP] = ftplib.FTP
    ) -> TestResult | None:
        """Log in to ftpd and look for the Atmosphere folder; remember the result."""
        target = self.credentials(owner_id, switch_id)
        if target is None:
            return None
        result = check_connection(target, self.resolver, ftp_factory)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE switches SET last_test_at = ?, last_test_ok = ?, last_test_message = ?"
                " WHERE id = ? AND owner_id = ?",
                (utc_now(), int(result.ok), result.message, switch_id, owner_id),
            )
        return result


def _public(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["has_password"] = bool(out["has_password"])
    if out["last_test_ok"] is not None:
        out["last_test_ok"] = bool(out["last_test_ok"])
    return out


def connect(
    target: dict[str, Any], resolver: Resolver, ftp_factory: Callable[..., ftplib.FTP]
) -> ftplib.FTP:
    """Open and log in to ftpd at a checked local address. Errors are SwitchError."""
    host, port = target["host"], target["port"]
    address = lan_address(host, port, resolver)
    ftp = ftp_factory(timeout=CONNECT_TIMEOUT)
    try:
        ftp.connect(address, port)
    except TimeoutError:
        raise SwitchError(
            f"no answer from {host}:{port}. Is ftpd open on the Switch, and is the address right?"
        ) from None
    except ConnectionRefusedError:
        raise SwitchError(
            f"{host} refused the connection on port {port}: ftpd isn't running, or uses "
            "another port"
        ) from None
    except OSError as exc:
        raise SwitchError(f"couldn't connect to {host}:{port} ({exc.strerror or exc})") from None
    try:
        # ftpd accepts any login unless one is configured in it.
        ftp.login(target.get("username") or "anonymous", target.get("password") or "")
    except ftplib.error_perm:
        _close(ftp)
        raise SwitchError("ftpd refused the login: check the username and password") from None
    except (ftplib.Error, OSError) as exc:
        _close(ftp)
        raise SwitchError(f"the login to ftpd failed ({exc})") from None
    return ftp


def check_connection(
    target: dict[str, Any],
    resolver: Resolver = resolve,
    ftp_factory: Callable[..., ftplib.FTP] = ftplib.FTP,
) -> TestResult:
    try:
        ftp = connect(target, resolver, ftp_factory)
    except SwitchError as exc:
        return TestResult(False, _sentence(str(exc)))
    try:
        ftp.cwd("/atmosphere")
    except ftplib.error_perm:
        return TestResult(
            False,
            "Connected to ftpd, but there's no /atmosphere folder: is this a Switch running "
            "Atmosphere?",
        )
    except (ftplib.Error, OSError) as exc:
        return TestResult(False, f"Connected to ftpd, but it stopped responding ({exc}).")
    finally:
        _close(ftp)
    return TestResult(True, "Connected to ftpd and found the Atmosphere folder.")


def _close(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except (ftplib.Error, OSError, EOFError):
        ftp.close()


def _sentence(text: str) -> str:
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "?", "!")) else text + "."
