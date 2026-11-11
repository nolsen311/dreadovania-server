"""FastAPI application factory."""

import asyncio
import contextlib
import ipaddress
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.metadata import version
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from dreadovania import __version__, auth, problems, routes_api, routes_auth, ui
from dreadovania.app_updates import AppUpdates
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.jobs import JobQueue
from dreadovania.presets import PresetStore
from dreadovania.romfs import RomfsInspector
from dreadovania.romfs_upload import RomfsUploads
from dreadovania.runner import RandovaniaRunner, Runner
from dreadovania.seeds import SeedStore
from dreadovania.switches import SwitchStore

API_PREFIX = routes_auth.API_PREFIX
log = logging.getLogger(__name__)

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
    if app.state.romfs is None:
        app.state.romfs = RomfsInspector(settings.romfs_path)
    queue = JobQueue(
        db,
        app.state.runner
        or RandovaniaRunner(
            timeout=settings.job_timeout_seconds, export_timeout=settings.export_timeout_seconds
        ),
        settings.data_dir,
        randovania_version=version("randovania"),
        max_queued_per_owner=settings.max_queued_jobs_per_user,
        romfs=app.state.romfs,
    )
    queue.recover_interrupted()
    if auth.setup_required(db):
        _announce_setup_link(settings, auth.create_setup_token(db))
    worker = asyncio.create_task(queue.run_worker(), name="job-worker")
    app.state.db, app.state.jobs = db, queue
    app.state.presets = PresetStore(db)
    app.state.seeds = SeedStore(db, settings.data_dir)
    app.state.switches = SwitchStore(db)
    if app.state.app_updates is None:
        app.state.app_updates = AppUpdates(settings.data_dir, enabled=settings.app_updates)
    uploads = RomfsUploads(
        settings.data_dir,
        app.state.romfs,
        max_bytes=settings.max_romfs_upload_bytes,
        max_files=settings.max_romfs_files,
        chunk_size=settings.romfs_upload_chunk_bytes,
        is_busy=queue.export_running,
    )
    # Never swap the game files under a running patch, nor patch during a swap.
    queue.romfs_busy = lambda: uploads.installing
    uploads.recover()
    app.state.romfs_uploads = uploads
    try:
        yield
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


def _announce_setup_link(settings: Settings, token: str) -> None:
    """Print the one-time setup link (ADR 0004). Only its hash is stored.

    The token goes in the URL fragment (after #), which browsers never send to
    the server, so it can't end up in access logs or proxy logs.
    """
    base = (
        auth.origin_of(str(settings.public_url))
        if settings.public_url is not None
        else f"{'https' if settings.tls_cert_file else 'http'}://<this-server>:{settings.port}"
    )
    lifetime = int(auth.SETUP_TOKEN_LIFETIME.total_seconds() // 60)
    log.warning(
        "\n%s\nFIRST-RUN SETUP: open this link to create your account (valid %d minutes;"
        " restart the server for a new one):\n\n    %s/setup#token=%s\n%s",
        "=" * 72, lifetime, base, token, "=" * 72,
    )  # fmt: skip


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _hostname_redirect(request: Request) -> str | None:
    """Where to send a request made to an IP address, if the server has a hostname."""
    requested = request.url.hostname
    if not requested or not _is_ip(requested):
        return None
    if getattr(request.app.state, "db", None) is None:
        return None  # not started
    public = routes_auth.public_origin(request)  # DREADOVANIA_PUBLIC_URL, else the wizard's
    if public is None:
        return None
    public_host = urlsplit(public).hostname
    if not public_host or _is_ip(public_host):
        return None  # the server is set up to be used by address
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{public}{request.url.path}{query}"


def create_app(
    settings: Settings | None = None,
    runner: Runner | None = None,
    romfs: RomfsInspector | None = None,
    app_updates: AppUpdates | None = None,
) -> FastAPI:
    """Build the app. Tests pass a fake ``runner`` and ``romfs``; production leaves them unset."""
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
    app.state.runner = runner
    app.state.romfs = romfs
    app.state.app_updates = app_updates
    app.state.login_limiter = auth.AttemptLimiter(**routes_auth.LOGIN_LIMIT)
    app.state.setup_limiter = auth.AttemptLimiter(**routes_auth.SETUP_LIMIT)
    app.state.passkey_start_limiter = auth.AttemptLimiter(**routes_auth.PASSKEY_START_LIMIT)
    app.state.pair_limiter = auth.AttemptLimiter(**routes_auth.PAIR_LIMIT)
    app.state.invite_limiter = auth.AttemptLimiter(**routes_auth.INVITE_LIMIT)
    problems.install(app)
    app.include_router(routes_auth.public)
    app.include_router(routes_auth.protected)
    app.include_router(routes_api.router)
    ui.install(app)

    @app.middleware("http")
    async def hostname_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Send a browser that opened the server by IP address to its hostname (ADR 0005).

        Passkeys and cookies belong to the hostname, so an IP address can't sign
        in. Only page loads (GET/HEAD) are redirected; /healthz stays reachable
        by address for Docker's health check.
        """
        if request.method in ("GET", "HEAD") and request.url.path != "/healthz":
            target = _hostname_redirect(request)
            if target is not None:
                return RedirectResponse(target, status_code=308)
        return await call_next(request)

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
