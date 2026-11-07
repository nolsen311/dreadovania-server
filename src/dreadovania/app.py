"""FastAPI application factory."""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from dreadovania import __version__

API_PREFIX = "/api/v1"

# Baseline headers for every response. The CSP is strict because the UI will be a
# static PWA served from this origin, with no third-party scripts or styles.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
        "object-src 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class Health(BaseModel):
    status: str


class ServerInfo(BaseModel):
    name: str
    version: str


def create_app() -> FastAPI:
    app = FastAPI(
        title="Dreadovania Server",
        version=__version__,
        # The OpenAPI document is the contract for dreadovania-nx (ADR 0007), so it is
        # always served. The interactive docs UIs are off: they load third-party assets.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.get("/healthz", tags=["meta"])
    def healthz() -> Health:
        """Liveness check for Docker and reverse proxies."""
        return Health(status="ok")

    @app.get(f"{API_PREFIX}/info", tags=["meta"])
    def info() -> ServerInfo:
        """Server name and version."""
        return ServerInfo(name="dreadovania", version=__version__)

    return app
