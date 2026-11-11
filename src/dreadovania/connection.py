# SPDX-License-Identifier: GPL-3.0-or-later
"""Tells the app about the connection a request came on (ADR 0023).

Uvicorn doesn't pass the client's TLS certificate to the application, and its
forwarded-headers support replaces the connection's address with the one a
trusted proxy reports. Secure remote access needs both originals: the
certificate the TLS layer verified, and the address of the proxy that sent the
request, so forwarded certificate headers are only read from trusted proxies.

This HTTP protocol wraps uvicorn's own and puts a ``remote_access.Peer`` in each
request's ASGI scope, before uvicorn's forwarded-headers middleware runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from uvicorn.protocols.http.auto import AutoHTTPProtocol

from dreadovania.remote_access import PEER_KEY, Peer


def protocol_class(*, front_door: bool = False) -> type[asyncio.Protocol]:
    """Uvicorn's HTTP protocol, recording each connection's peer and certificate."""

    class PeerProtocol(AutoHTTPProtocol):  # type: ignore[misc, valid-type]
        def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            ssl_object = transport.get_extra_info("ssl_object")
            # Only present when the TLS layer asked for a certificate and verified it.
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
            client = getattr(self, "client", None)
            peer = Peer(host=client[0] if client else None, client_cert=der, front_door=front_door)
            app = self.app

            async def with_peer(scope: Any, receive: Any, send: Any) -> None:
                scope[PEER_KEY] = peer
                await app(scope, receive, send)

            self.app = with_peer

    PeerProtocol.__name__ = "FrontDoorProtocol" if front_door else "PeerProtocol"
    return PeerProtocol
