"""Entry point: ``python -m dreadovania``.

``python -m dreadovania remote-access off`` turns secure remote access off, for
an owner who can no longer reach the server (ADR 0023). In Docker:
``docker compose exec dreadovania python -m dreadovania remote-access off``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import ssl
import sys
from collections.abc import Iterator
from typing import Any

import uvicorn
from fastapi import FastAPI

from dreadovania import connection, tls
from dreadovania.app import create_app
from dreadovania.config import Settings

log = logging.getLogger("dreadovania")


def client_auth_for(app: FastAPI, settings: Settings) -> tls.ClientAuth:
    """The built-in CA's files, and the setting, once the app has started."""
    folder = settings.data_dir / "remote-access"

    def verify_mode() -> ssl.VerifyMode:
        remote = getattr(app.state, "remote_access", None)
        return remote.verify_mode() if remote is not None else ssl.CERT_NONE

    def refresh() -> None:
        certs = getattr(app.state, "client_certs", None)
        if certs is not None and certs.ca_cert_path.is_file():
            certs.crl_pem()

    return tls.ClientAuth(
        ca=folder / "ca.pem", crl=folder / "crl.pem", verify_mode=verify_mode, refresh=refresh
    )


def uvicorn_configs(settings: Settings) -> tuple[uvicorn.Config, uvicorn.Config | None]:
    """The main port's settings, and the front-door port's when it has one.

    HTTPS when certificate files are set (ADR 0005). The front door (ADR 0023)
    is only needed when the server checks client certificates itself, so it
    listens only when the server serves HTTPS; it never asks for a certificate.
    """
    pair = tls.check_pair(settings.tls_cert_file, settings.tls_key_file)
    app = create_app(settings)
    proxies = settings.trusted_proxy_list
    common: dict[str, Any] = {
        "host": settings.host,
        # Forwarded headers only from the proxies the user lists (ADR 0005).
        "proxy_headers": bool(proxies),
        "forwarded_allow_ips": proxies or [],
        "server_header": False,
        "date_header": False,
    }
    if pair is None:
        main = uvicorn.Config(app, port=settings.port, http=connection.protocol_class(), **common)
        return main, None
    reloader = tls.CertificateReloader(*pair, client_auth=client_auth_for(app, settings))
    app.state.tls_reloader = reloader

    def main_context(_config: uvicorn.Config, _default: object) -> ssl.SSLContext:
        return reloader.context

    def front_door_context(_config: uvicorn.Config, _default: object) -> ssl.SSLContext:
        return reloader.front_door_context

    main = uvicorn.Config(
        app, port=settings.port, http=connection.protocol_class(),
        ssl_context_factory=main_context, **common,
    )  # fmt: skip
    if not settings.front_door_port:
        return main, None
    front = uvicorn.Config(
        app, port=settings.front_door_port, http=connection.protocol_class(front_door=True),
        ssl_context_factory=front_door_context, lifespan="off", **common,
    )  # fmt: skip
    return main, front


def uvicorn_config(settings: Settings) -> uvicorn.Config:
    """The main port's settings."""
    return uvicorn_configs(settings)[0]


class _FrontDoorServer(uvicorn.Server):
    """Runs beside the main server, which handles the signals for both."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _serve(main: uvicorn.Config, front: uvicorn.Config | None) -> None:
    server = uvicorn.Server(main)
    if front is None:
        await server.serve()
        return
    door = _FrontDoorServer(front)
    task = asyncio.create_task(door.serve(), name="front-door")
    try:
        await server.serve()
    finally:
        door.should_exit = True
        await task


def _remote_access_off(settings: Settings) -> int:
    from dreadovania.client_certs import ClientCertificates
    from dreadovania.db import Database
    from dreadovania.remote_access import RemoteAccess

    db = Database(settings.data_dir / "dreadovania.db")
    db.migrate()
    remote = RemoteAccess(
        db, ClientCertificates(db, settings.data_dir),
        serves_tls=settings.tls_cert_file is not None, trusted_proxies=[],
    )  # fmt: skip
    remote.turn_off(None)
    print("Secure remote access is off. The server picks this up within a few seconds.")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m dreadovania")
    sub = parser.add_subparsers(dest="command")
    ra = sub.add_parser("remote-access", help="turn secure remote access off (ADR 0023)")
    ra.add_argument("action", choices=["off"])
    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "remote-access":
        sys.exit(_remote_access_off(settings))
    try:
        main_config, front = uvicorn_configs(settings)
    except tls.TlsError as exc:
        logging.basicConfig()
        log.error("HTTPS can't start: %s", exc)
        sys.exit(2)
    asyncio.run(_serve(main_config, front), loop_factory=main_config.get_loop_factory())


if __name__ == "__main__":
    main()
