# SPDX-License-Identifier: GPL-3.0-or-later
"""The mobile web UI: server-rendered Jinja pages with htmx (ADR 0011, PR 4).

These routes are not part of the API contract (they're excluded from OpenAPI).
They reuse the same stores and the same sign-in flows as the API, so the same
rules apply: sessions, Origin checks on every change, rate limits, owner scoping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from dreadovania import auth
from dreadovania.jobs import QueueFull
from dreadovania.problems import ApiProblem
from dreadovania.romfs import SUPPORTED_VERSIONS
from dreadovania.routes_auth import (
    SAFE_METHODS,
    check_origin,
    perform_login,
    perform_logout,
    perform_setup,
    public_origin,
)
from dreadovania.seeds import NoSpoiler, SeedBusy, SeedNotReady
from dreadovania.validation import InvalidInput

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
router = APIRouter(include_in_schema=False)

MAX_FORM_BYTES = 16 * 1024
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


# ---- helpers ----------------------------------------------------------------------------


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def go(request: Request, url: str) -> Response:
    """Navigate: htmx follows HX-Redirect; a plain form post follows a 303."""
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


async def read_form(request: Request) -> dict[str, str]:
    """A small URL-encoded form, read with a size limit (no multipart parser needed)."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            raise ApiProblem(413, "Form too large")
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in fields.items()}


def page_session(request: Request) -> auth.Session:
    """The signed-in user for a page, or a redirect to setup or sign-in."""
    db = request.app.state.db
    if auth.setup_required(db):
        raise Redirect("/setup")
    token = request.cookies.get("dreadovania_session")
    session = auth.resolve_session(db, token) if token else None
    if session is None:
        raise Redirect("/login")
    if request.method not in SAFE_METHODS:
        check_origin(request, public_origin(request), required=True)
    return session


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def install(app: Any) -> None:
    from fastapi.staticfiles import StaticFiles

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect) -> Response:
        return go(request, exc.url)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)


# ---- first run and sign-in ---------------------------------------------------------------


@router.get("/setup")
def setup_page(request: Request) -> Response:
    if not auth.setup_required(request.app.state.db):
        return go(request, "/login")
    return render(request, "setup.html", suggested_url=public_origin(request) or "")


@router.post("/setup")
async def setup_submit(request: Request) -> Response:
    form = await read_form(request)
    response = go(request, "/")
    try:
        perform_setup(
            request,
            response,
            form.get("token", "")[:128],
            form.get("username", "")[:64],
            form.get("password", "")[: auth.MAX_PASSWORD_LENGTH],
            form.get("public_url", "")[:2048],
        )
    except ApiProblem as problem:
        return render(request, "_error.html", message=problem.detail or problem.title)
    return response


@router.get("/login")
def login_page(request: Request) -> Response:
    if auth.setup_required(request.app.state.db):
        return go(request, "/setup")
    return render(request, "login.html")


@router.post("/login")
async def login_submit(request: Request) -> Response:
    form = await read_form(request)
    response = go(request, "/")
    try:
        perform_login(request, response, form.get("username", ""), form.get("password", ""))
    except ApiProblem as problem:
        return render(request, "_error.html", message=problem.detail or problem.title)
    return response


@router.post("/logout")
def logout_submit(request: Request) -> Response:
    page_session(request)
    response = go(request, "/login")
    perform_logout(request, response)
    return response


# ---- generating ------------------------------------------------------------------------------


@router.get("/")
def home(request: Request) -> Response:
    user = page_session(request)
    return render(
        request,
        "home.html",
        user=user,
        presets=request.app.state.presets.list(user.user_id),
        seeds=request.app.state.seeds.list(user.user_id, 20),
    )


@router.post("/seeds")
async def generate(request: Request) -> Response:
    user = page_session(request)
    form = await read_form(request)
    from_permalink = form.get("source") == "permalink"
    seed_text = form.get("seed_number", "").strip()
    if seed_text and not seed_text.isdigit():
        return render(request, "_error.html", message="the seed number must be a whole number")
    try:
        sub = request.app.state.jobs.submit_generate(
            user.user_id,
            preset_key=None if from_permalink else (form.get("preset_id") or None),
            permalink=form.get("permalink") if from_permalink else None,
            seed_number=int(seed_text) if seed_text and not from_permalink else None,
            race=form.get("race") == "on" and not from_permalink,
        )
    except (InvalidInput, QueueFull) as exc:
        return render(request, "_error.html", message=str(exc))
    return go(request, f"/seeds/{sub.seed_id}")


@router.get("/seeds/{seed_id}")
def seed_page(seed_id: str, request: Request) -> Response:
    user = page_session(request)
    seed = request.app.state.seeds.get(user.user_id, seed_id)
    if seed is None:
        return render(request, "not_found.html", status_code=404, user=user)
    return render(request, "seed.html", user=user, seed=seed, job=_job_for(request, user, seed))


def _job_for(request: Request, user: auth.Session, seed: dict) -> dict | None:
    return request.app.state.jobs.get_job(seed["job_id"], user.user_id) if seed["job_id"] else None


@router.get("/seeds/{seed_id}/status")
def seed_status(seed_id: str, request: Request) -> Response:
    """The part of the seed page that changes while it generates (polled by htmx)."""
    user = page_session(request)
    seed = request.app.state.seeds.get(user.user_id, seed_id)
    if seed is None:
        return render(request, "_error.html", status_code=404, message="seed not found")
    return render(request, "_seed_status.html", seed=seed, job=_job_for(request, user, seed))


@router.get("/seeds/{seed_id}/spoiler")
def seed_spoiler(seed_id: str, request: Request) -> Response:
    user = page_session(request)
    try:
        spoiler = request.app.state.seeds.spoiler(user.user_id, seed_id)
    except NoSpoiler:
        return render(request, "_error.html", message="race seeds have no spoiler")
    except SeedNotReady:
        return render(request, "_error.html", message="the seed isn't ready yet")
    if spoiler is None:
        return render(request, "_error.html", status_code=404, message="seed not found")
    regions: dict[str, list[dict]] = {}
    for loc in spoiler["locations"]:
        regions.setdefault(loc["region"], []).append(loc)
    return render(request, "_spoiler.html", spoiler=spoiler, regions=regions)


@router.post("/seeds/{seed_id}/delete")
def seed_delete(seed_id: str, request: Request) -> Response:
    user = page_session(request)
    try:
        request.app.state.seeds.delete(user.user_id, seed_id)
    except SeedBusy:
        return render(request, "_error.html", message="cancel the generation first")
    return go(request, "/")


@router.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: str, request: Request) -> Response:
    user = page_session(request)
    request.app.state.jobs.cancel(job_id, user.user_id)
    job = request.app.state.jobs.get_job(job_id, user.user_id)
    return go(request, f"/seeds/{job['seed_id']}" if job else "/")


# ---- purge ----------------------------------------------------------------------------------


def _purge_days(form: dict[str, str]) -> int | None:
    if form.get("scope") == "all":
        return None
    days = form.get("days", "").strip()
    if form.get("scope") == "older_than" and days.isdigit() and 1 <= int(days) <= 3650:
        return int(days)
    raise InvalidInput("choose all seeds, or seeds older than 1-3650 days")


@router.post("/seeds/purge/preview")
async def purge_preview(request: Request) -> Response:
    """Step one: say exactly how many seeds would be deleted, and ask to confirm."""
    user = page_session(request)
    form = await read_form(request)
    try:
        days = _purge_days(form)
    except InvalidInput as exc:
        return render(request, "_error.html", message=str(exc))
    count = request.app.state.seeds.count_purgeable(user.user_id, days)
    return render(request, "_purge_confirm.html", count=count, days=days)


@router.post("/seeds/purge")
async def purge(request: Request) -> Response:
    user = page_session(request)
    form = await read_form(request)
    try:
        days = _purge_days(form)
    except InvalidInput as exc:
        return render(request, "_error.html", message=str(exc))
    if form.get("confirm") != "yes":
        return render(request, "_error.html", message="the purge wasn't confirmed")
    request.app.state.seeds.purge(user.user_id, days)
    return go(request, "/")


# ---- game files -------------------------------------------------------------------------------


@router.get("/game-files")
async def game_files_page(request: Request) -> Response:
    user = page_session(request)
    status = await run_in_threadpool(request.app.state.romfs.status)
    uploads = request.app.state.romfs_uploads
    return render(
        request,
        "game_files.html",
        user=user,
        romfs=status,
        supported=SUPPORTED_VERSIONS,
        upload=uploads.current(),
        upload_blocked=await run_in_threadpool(uploads.unavailable_reason),
    )


# ---- presets ----------------------------------------------------------------------------------


@router.get("/presets")
def presets_page(request: Request) -> Response:
    user = page_session(request)
    return render(
        request, "presets.html", user=user, presets=request.app.state.presets.list(user.user_id)
    )


@router.post("/presets/{preset_id}/delete")
def preset_delete(preset_id: str, request: Request) -> Response:
    user = page_session(request)
    request.app.state.presets.delete(user.user_id, preset_id)
    return go(request, "/presets")
