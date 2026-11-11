# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTPS from mounted certificate files, reloaded when they change (ADR 0005).

The user mounts ``fullchain.pem`` and ``privkey.pem`` from certbot, acme.sh, a
Caddy sidecar or their proxy. Renewal tools replace those files every few
weeks, so the server checks them on new connections (at most once every
``CHECK_INTERVAL`` seconds) and switches to the new certificate without a
restart. A half-written or broken pair is ignored, keeping the current one.

With secure remote access (ADR 0023), the same contexts also carry the built-in
CA and its revocation list, reloaded the same way, and the main port asks
clients for a certificate as the setting says. A second context, for the
front-door port, never asks.
"""

from __future__ import annotations

import logging
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CHECK_INTERVAL = 5.0  # seconds


class TlsError(Exception):
    """The certificate files can't be used. The message says what to fix."""


@dataclass(frozen=True)
class ClientAuth:
    """Where client certificates are checked against, and whether to ask for one."""

    ca: Path
    crl: Path
    verify_mode: Callable[[], ssl.VerifyMode]
    # Brings crl.pem up to date (it is republished daily, and on every revocation).
    refresh: Callable[[], object] = lambda: None


@dataclass(frozen=True)
class _Stamp:
    cert: tuple[int, int, int]
    key: tuple[int, int, int]
    ca: tuple[int, int, int] | None = None
    crl: tuple[int, int, int] | None = None


def _one(path: Path) -> tuple[int, int, int]:
    st = path.stat()  # follows symlinks, as certbot's live/ folder uses them
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _maybe(path: Path) -> tuple[int, int, int] | None:
    try:
        return _one(path)
    except OSError:
        return None


def _stamp(cert: Path, key: Path, client_auth: ClientAuth | None = None) -> _Stamp:
    if client_auth is None:
        return _Stamp(_one(cert), _one(key))
    return _Stamp(_one(cert), _one(key), _maybe(client_auth.ca), _maybe(client_auth.crl))


def build_context(cert: Path, key: Path, client_auth: ClientAuth | None = None) -> ssl.SSLContext:
    """A server context for the pair: TLS 1.2 or newer.

    With ``client_auth`` and an existing CA, it also trusts that CA for client
    certificates and loads its revocation list. Whether a certificate is asked
    for is set on the listening context, not here (see ``CertificateReloader``).
    """
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=cert, keyfile=key)
    except FileNotFoundError as exc:
        raise TlsError(f"the certificate file {exc.filename} doesn't exist") from None
    except PermissionError as exc:
        raise TlsError(f"the server can't read {exc.filename}: check its permissions") from None
    except ssl.SSLError as exc:
        raise TlsError(
            f"the certificate and key can't be used together ({exc.reason or exc}): mount the "
            "full chain (fullchain.pem) and its private key (privkey.pem)"
        ) from None
    if client_auth is not None and client_auth.ca.is_file():
        try:
            context.load_verify_locations(cafile=client_auth.ca)
            if client_auth.crl.is_file():  # written once the app has started
                context.load_verify_locations(cafile=client_auth.crl)
        except (OSError, ssl.SSLError) as exc:
            raise TlsError(
                f"the client certificate CA or revocation list can't be used ({exc})"
            ) from None
    return context


def _listening_context(cert: Path, key: Path) -> ssl.SSLContext:
    return build_context(cert, key)


class CertificateReloader:
    """Serves the current certificate pair, reloading it when the files change.

    ``context`` is the context the main port listens with, and
    ``front_door_context`` the front-door port's (ADR 0023). Their SNI callback,
    which runs on every new connection, hands the connection the newest working
    certificate, with the client CA and revocation list when there are any.

    Whether a client certificate is asked for can't be changed by handing over
    another context (OpenSSL keeps the listening context's verify mode and
    flags), so ``apply_client_auth`` sets them on ``context`` itself, for the
    connections that follow.
    """

    def __init__(
        self,
        cert: Path,
        key: Path,
        *,
        interval: float = CHECK_INTERVAL,
        client_auth: ClientAuth | None = None,
    ) -> None:
        self.cert, self.key, self.interval = cert, key, interval
        self.client_auth = client_auth
        self._lock = threading.Lock()
        self._refresh_client_files()
        # Fail at startup, not on first request.
        self._current = build_context(cert, key, client_auth)
        self._stamp = _stamp(cert, key, client_auth)
        self._checked = time.monotonic()
        self.context = _listening_context(cert, key)
        self.context.sni_callback = self._on_hello
        self.front_door_context = _listening_context(cert, key)
        self.front_door_context.sni_callback = self._on_hello
        self.apply_client_auth()

    @property
    def current(self) -> ssl.SSLContext:
        return self._current

    def _refresh_client_files(self) -> None:
        if self.client_auth is None:
            return
        try:
            self.client_auth.refresh()
        except Exception:  # never stop serving over the revocation list
            log.exception("Couldn't bring the client certificate revocation list up to date")

    def apply_client_auth(self) -> ssl.VerifyMode:
        """Ask for client certificates on the main port as the setting says."""
        mode = ssl.CERT_NONE
        if self.client_auth is not None:
            try:
                mode = self.client_auth.verify_mode()
            except Exception:
                log.exception("Couldn't read the secure remote access setting")
                mode = ssl.CERT_REQUIRED if self.context.verify_mode else ssl.CERT_NONE
        if mode != ssl.CERT_NONE and not self.client_auth.ca.is_file():  # type: ignore[union-attr]
            mode = ssl.CERT_NONE  # nothing to check against yet
        self.context.verify_mode = mode
        # Without a revocation list loaded, checking against one would refuse everyone;
        # the app still refuses revoked certificates (remote_access.RemoteAccess.check).
        if mode == ssl.CERT_NONE or not self.client_auth.crl.is_file():  # type: ignore[union-attr]
            self.context.verify_flags &= ~ssl.VERIFY_CRL_CHECK_LEAF
        else:
            self.context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
        return mode

    def check(self, *, force: bool = False) -> bool:
        """Reload if the files changed; returns whether a new certificate is in use."""
        now = time.monotonic()
        with self._lock:
            if not force and now - self._checked < self.interval:
                return False
            self._checked = now
            self._refresh_client_files()
            self.apply_client_auth()
            try:
                stamp = _stamp(self.cert, self.key, self.client_auth)
            except OSError:
                return False  # mid-renewal: a file is briefly missing
            if stamp == self._stamp:
                return False
            try:
                context = build_context(self.cert, self.key, self.client_auth)
            except TlsError as exc:
                # e.g. the new certificate is written but its key isn't yet.
                log.warning("Keeping the current certificate: %s", exc)
                return False
            self._current, self._stamp = context, stamp
        log.info("Loaded the renewed TLS certificate or client certificate files")
        return True

    def _on_hello(
        self, connection: ssl.SSLObject, _server_name: str | None, _context: ssl.SSLContext
    ) -> None:
        self.check()
        connection.context = self._current


def check_pair(cert: Path | None, key: Path | None) -> tuple[Path, Path] | None:
    """Both files or neither: half a configuration is a mistake worth stopping for."""
    if cert is None and key is None:
        return None
    if cert is None or key is None:
        raise TlsError(
            "set both DREADOVANIA_TLS_CERT_FILE (fullchain.pem) and DREADOVANIA_TLS_KEY_FILE "
            "(privkey.pem), or neither"
        )
    return cert, key
