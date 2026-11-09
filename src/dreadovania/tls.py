# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTPS from mounted certificate files, reloaded when they change (ADR 0005).

The user mounts ``fullchain.pem`` and ``privkey.pem`` from certbot, acme.sh, a
Caddy sidecar or their proxy. Renewal tools replace those files every few
weeks, so the server checks them on new connections (at most once every
``CHECK_INTERVAL`` seconds) and switches to the new certificate without a
restart. A half-written or broken pair is ignored, keeping the current one.
"""

from __future__ import annotations

import logging
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CHECK_INTERVAL = 5.0  # seconds


class TlsError(Exception):
    """The certificate files can't be used. The message says what to fix."""


@dataclass(frozen=True)
class _Stamp:
    cert: tuple[int, int, int]
    key: tuple[int, int, int]


def _stamp(cert: Path, key: Path) -> _Stamp:
    def one(path: Path) -> tuple[int, int, int]:
        st = path.stat()  # follows symlinks, as certbot's live/ folder uses them
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    return _Stamp(one(cert), one(key))


def build_context(cert: Path, key: Path) -> ssl.SSLContext:
    """A server context for the pair: TLS 1.2 or newer, no client certificates."""
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
    return context


class CertificateReloader:
    """Serves the current certificate pair, reloading it when the files change.

    ``context`` is the one context the server listens with. Its SNI callback,
    which runs on every new connection, hands the connection the newest
    working certificate.
    """

    def __init__(self, cert: Path, key: Path, *, interval: float = CHECK_INTERVAL) -> None:
        self.cert, self.key, self.interval = cert, key, interval
        self._lock = threading.Lock()
        self._current = build_context(cert, key)  # fail at startup, not on first request
        self._stamp = _stamp(cert, key)
        self._checked = time.monotonic()
        self.context = build_context(cert, key)
        self.context.sni_callback = self._on_hello

    @property
    def current(self) -> ssl.SSLContext:
        return self._current

    def check(self, *, force: bool = False) -> bool:
        """Reload if the files changed; returns whether a new certificate is in use."""
        now = time.monotonic()
        with self._lock:
            if not force and now - self._checked < self.interval:
                return False
            self._checked = now
            try:
                stamp = _stamp(self.cert, self.key)
            except OSError:
                return False  # mid-renewal: a file is briefly missing
            if stamp == self._stamp:
                return False
            try:
                context = build_context(self.cert, self.key)
            except TlsError as exc:
                # e.g. the new certificate is written but its key isn't yet.
                log.warning("Keeping the current certificate: %s", exc)
                return False
            self._current, self._stamp = context, stamp
        log.info("Loaded the renewed TLS certificate")
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
