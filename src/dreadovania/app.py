"""FastAPI application factory."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from dreadovania import __version__
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.jobs import JobQueue
from dreadovania.runner import RandovaniaRunner

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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open storage and run the job worker for the lifetime of the server."""
    settings: Settings = app.state.settings
    db = Database(settings.data_dir / "dreadovania.db")
    db.migrate()
    queue = JobQueue(
        db,
        RandovaniaRunner(timeout=settings.job_timeout_seconds),
        settings.data_dir,
        randovania_version=version("randovania"),
        max_queued_per_owner=settings.max_queued_jobs_per_user,
    )
    queue.recover_interrupted()
    worker = asyncio.create_task(queue.run_worker(), name="job-worker")
    app.state.db, app.state.jobs = db, queue
    try:
        yield
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        lifespan=_lifespan,
        title="Dreadovania Server",
        version=__version__,
        # The OpenAPI document is the contract for dreadovania-nx (ADR 0007), so it is
        # always served. The interactive docs UIs are off: they load third-party assets.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings if settings is not None else Settings()

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
