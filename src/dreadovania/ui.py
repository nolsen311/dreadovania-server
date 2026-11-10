# SPDX-License-Identifier: GPL-3.0-or-later
"""The mobile web UI: server-rendered Jinja pages with htmx (ADR 0011, PR 4).

These routes are not part of the API contract (they're excluded from OpenAPI).
They reuse the same stores and the same sign-in flows as the API, so the same
rules apply: sessions, Origin checks on every change, rate limits, owner scoping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from dreadovania import auth, devices, passkeys, rdv_presets
from dreadovania.jobs import CannotPatch, CannotSend, QueueFull
from dreadovania.problems import ApiProblem
from dreadovania.rdv_presets import PresetProblem
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
from dreadovania.switches import DEFAULT_PORT, SwitchError
from dreadovania.validation import InvalidInput

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["preset_text"] = rdv_presets.description_text
router = APIRouter(include_in_schema=False)

MAX_FORM_BYTES = 16 * 1024
MAX_PICKUP_FORM_BYTES = 64 * 1024  # the Pickup pool tab sends a few hundred fields
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


async def read_form(request: Request, limit: int = MAX_FORM_BYTES) -> dict[str, str]:
    """A small URL-encoded form, read with a size limit (no multipart parser needed)."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
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
    return render(request, "login.html", passkeys_unavailable=_passkeys_unavailable(request))


def _passkeys_unavailable(request: Request) -> str | None:
    """Why this server can't use passkeys, or None when it can."""
    try:
        passkeys.party_for(public_origin(request))
    except passkeys.PasskeyError as exc:
        return str(exc)
    return None


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
    return render(request, "seed.html", user=user, **_status_context(request, user, seed))


def _job_for(request: Request, user: auth.Session, seed: dict) -> dict | None:
    return request.app.state.jobs.get_job(seed["job_id"], user.user_id) if seed["job_id"] else None


@router.get("/seeds/{seed_id}/status")
def seed_status(seed_id: str, request: Request) -> Response:
    """The part of the seed page that changes while it generates (polled by htmx)."""
    user = page_session(request)
    seed = request.app.state.seeds.get(user.user_id, seed_id)
    if seed is None:
        return render(request, "_error.html", status_code=404, message="seed not found")
    return render(request, "_seed_status.html", **_status_context(request, user, seed))


def _status_context(request: Request, user: auth.Session, seed: dict) -> dict[str, Any]:
    """What the generation and patching parts of a seed page need."""
    context: dict[str, Any] = {"seed": seed, "job": _job_for(request, user, seed)}
    if seed["status"] == "ready":
        context["mod_job"] = (
            request.app.state.jobs.get_job(seed["mod_job_id"], user.user_id)
            if seed["mod_job_id"]
            else None
        )
        # Inspecting the game files walks the folder: only when patching could start.
        if seed["mod_status"] not in ("pending", "ready"):
            context["romfs"] = request.app.state.romfs.status()
        if seed["mod_status"] == "ready":
            context.update(_push_context(request, user, seed))
    return context


def _push_context(request: Request, user: auth.Session, seed: dict) -> dict[str, Any]:
    """The "Send to Switch" part of a patched seed's page (ADR 0016)."""
    switches = request.app.state.switches.list(user.user_id)
    push_job = (
        request.app.state.jobs.get_job(seed["push_job_id"], user.user_id)
        if seed.get("push_job_id")
        else None
    )
    push_switch = None
    if push_job and push_job.get("params"):
        switch_id = json.loads(push_job["params"]).get("switch_id")
        push_switch = next((s for s in switches if s["id"] == switch_id), None)
    return {"seed": seed, "switches": switches, "push_job": push_job, "push_switch": push_switch}


@router.post("/seeds/{seed_id}/push")
async def push_seed(seed_id: str, request: Request) -> Response:
    user = page_session(request)
    form = await read_form(request)
    try:
        request.app.state.jobs.submit_push(
            user.user_id,
            seed_id,
            form.get("switch_id", ""),
            overwrite=form.get("overwrite") == "on",
            clear=form.get("clear") == "on",
        )
    except LookupError:
        return render(request, "_error.html", status_code=404, message="seed not found")
    except (CannotSend, QueueFull) as exc:
        return render(request, "_error.html", message=str(exc))
    return go(request, f"/seeds/{seed_id}")


@router.get("/seeds/{seed_id}/push/status")
def push_status(seed_id: str, request: Request) -> Response:
    """The sending part of the seed page (polled by htmx while sending)."""
    user = page_session(request)
    seed = request.app.state.seeds.get(user.user_id, seed_id)
    if seed is None or seed["mod_status"] != "ready":
        return render(request, "_error.html", status_code=404, message="seed not found")
    return render(request, "_push_status.html", **_push_context(request, user, seed))


@router.get("/seeds/{seed_id}/mod/status")
def mod_status(seed_id: str, request: Request) -> Response:
    """The patching part of the seed page (polled by htmx while patching)."""
    user = page_session(request)
    seed = request.app.state.seeds.get(user.user_id, seed_id)
    if seed is None or seed["status"] != "ready":
        return render(request, "_error.html", status_code=404, message="seed not found")
    return render(request, "_mod_status.html", **_status_context(request, user, seed))


@router.post("/seeds/{seed_id}/patch")
def patch(seed_id: str, request: Request) -> Response:
    user = page_session(request)
    if request.app.state.seeds.get(user.user_id, seed_id) is None:
        return render(request, "_error.html", status_code=404, message="seed not found")
    try:
        request.app.state.jobs.submit_export(user.user_id, seed_id)
    except LookupError:
        return render(request, "_error.html", status_code=404, message="seed not found")
    except (CannotPatch, QueueFull) as exc:
        return render(request, "_error.html", message=str(exc))
    return go(request, f"/seeds/{seed_id}")


@router.post("/seeds/{seed_id}/mod/delete")
def mod_delete(seed_id: str, request: Request) -> Response:
    user = page_session(request)
    try:
        request.app.state.seeds.delete_mod(user.user_id, seed_id)
    except SeedBusy:
        return render(request, "_error.html", message="cancel the patching first")
    return go(request, f"/seeds/{seed_id}")


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
        return render(request, "_error.html", message="cancel the generation or patching first")
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


# ---- Switches (ADR 0016) -----------------------------------------------------------------------


@router.get("/switches")
def switches_page(request: Request) -> Response:
    user = page_session(request)
    return render(
        request, "switches.html", user=user, switches=request.app.state.switches.list(user.user_id)
    )


@router.post("/switches")
async def switch_add(request: Request) -> Response:
    user = page_session(request)
    form = await read_form(request)
    fields = {key: form.get(key, "") for key in ("name", "host", "port", "username", "password")}
    fields["port"] = fields["port"].strip() or str(DEFAULT_PORT)
    try:
        await run_in_threadpool(request.app.state.switches.create, user.user_id, fields)
    except SwitchError as exc:
        return render(request, "_error.html", message=str(exc))
    return go(request, "/switches")


@router.post("/switches/{switch_id}/test")
async def switch_test(switch_id: str, request: Request) -> Response:
    user = page_session(request)
    result = await run_in_threadpool(request.app.state.switches.test, user.user_id, switch_id)
    if result is None:
        return render(request, "_error.html", status_code=404, message="Switch not found")
    return render(request, "_switch_test.html", result=result)


@router.post("/switches/{switch_id}/delete")
def switch_delete(switch_id: str, request: Request) -> Response:
    user = page_session(request)
    request.app.state.switches.delete(user.user_id, switch_id)
    return go(request, "/switches")


# ---- account and passkeys (ADR 0004) -------------------------------------------------------------


@router.get("/account")
def account_page(request: Request) -> Response:
    user = page_session(request)
    return render(
        request, "account.html", user=user,
        passkeys=passkeys.list_passkeys(request.app.state.db, user.user_id),
        passkeys_unavailable=_passkeys_unavailable(request),
        devices=devices.list_devices(request.app.state.db, user.user_id),
        server_url=public_origin(request),
    )  # fmt: skip


@router.post("/account/devices/code")
def device_code(request: Request) -> Response:
    user = page_session(request)
    code, _expires = devices.new_pairing_code(request.app.state.db, user.user_id)
    return render(
        request, "_pairing_code.html", code=code, minutes=int(devices.CODE_LIFETIME.seconds // 60),
        server_url=public_origin(request),
    )  # fmt: skip


@router.post("/account/devices/{device_id}/delete")
def device_delete(device_id: str, request: Request) -> Response:
    user = page_session(request)
    devices.remove(request.app.state.db, user.user_id, device_id)
    return go(request, "/account")


@router.post("/account/passkeys/{passkey_id}/delete")
def passkey_delete(passkey_id: str, request: Request) -> Response:
    user = page_session(request)
    passkeys.remove(request.app.state.db, user.user_id, passkey_id)
    return go(request, "/account")


# ---- presets ----------------------------------------------------------------------------------


@router.get("/presets")
def presets_page(request: Request) -> Response:
    user = page_session(request)
    return render(
        request, "presets.html", user=user, presets=request.app.state.presets.list(user.user_id)
    )


@router.post("/presets/{preset_id}/copy")
def preset_copy(preset_id: str, request: Request) -> Response:
    user = page_session(request)
    try:
        info = request.app.state.presets.copy(user.user_id, preset_id)
    except PresetProblem as exc:
        return render(request, "_error.html", message=f"Can't copy this preset: {exc}")
    if info is None:
        return render(request, "_error.html", status_code=404, message="Preset not found")
    return go(request, f"/presets/{info.id}/edit")


def _editable(request: Request, user: auth.Session, preset_id: str) -> rdv_presets.Preset:
    """One of the user's own presets, through Randovania; bundled ones aren't edited."""
    store = request.app.state.presets
    info = store.get(user.user_id, preset_id)
    if info is None or info.builtin:
        raise ApiProblem(404, "Preset not found")
    preset = store.load(user.user_id, preset_id)
    if preset is None:  # deleted meanwhile
        raise ApiProblem(404, "Preset not found")
    return preset


def _tab_context(preset: rdv_presets.Preset, tab: rdv_presets.Tab) -> dict[str, Any]:
    context: dict[str, Any] = {
        "tab": tab,
        "values": {s.key: rdv_presets.form_value(preset, s) for s in tab.settings},
        "warnings": rdv_presets.warnings(preset),
    }
    if tab.custom == "doors":
        context["doors"] = rdv_presets.door_view(preset)
    if tab.custom == "transporters":
        context["transporters"] = rdv_presets.transporter_view(preset)
        context["mode_help"] = rdv_presets.TRANSPORTER_MODES[preset.configuration.teleporters.mode]
    if tab.custom == "locations":
        context["locations"] = rdv_presets.location_view(preset)
        context["split"] = (
            preset.configuration.available_locations.randomization_mode.value != "full"
        )
    if tab.custom == "start":
        context["starts"] = rdv_presets.start_view(preset)
        context["start_count"] = len(preset.configuration.starting_location.locations)
    if tab.custom == "tricks":
        context["tricks"] = rdv_presets.trick_view(preset)
        context["trick_levels"] = rdv_presets.TRICK_LEVELS
    if tab.custom == "pickups":
        view = rdv_presets.pickup_view(preset)
        context["pickups"] = view
        context["priorities"] = rdv_presets.PRIORITIES
        for s in (view.model_style, view.model_source):
            context["values"][s.key] = rdv_presets.form_value(preset, s)
    return context


@router.get("/presets/{preset_id}/edit")
def preset_edit_page(preset_id: str, request: Request, tab: str = "generation") -> Response:
    user = page_session(request)
    try:
        preset = _editable(request, user, preset_id)
    except ApiProblem:
        return render(request, "not_found.html", status_code=404, user=user)
    except PresetProblem as exc:
        return render(request, "_error.html", status_code=409, message=str(exc))
    found = rdv_presets.tab(tab)
    if found is None or not found.ready:
        found = next(t for t in rdv_presets.TABS if t.ready)
    return render(
        request, "preset_edit.html", user=user, preset_id=preset_id, tabs=rdv_presets.TABS,
        preset_name=preset.name, description=preset.description, **_tab_context(preset, found),
    )  # fmt: skip


@router.post("/presets/{preset_id}/edit/about")
async def preset_edit_about(preset_id: str, request: Request) -> Response:
    user = page_session(request)
    form = await read_form(request)
    store = request.app.state.presets
    try:
        preset = _editable(request, user, preset_id)
        updated = rdv_presets.renamed(
            preset,
            form.get("name", "").strip(),
            rdv_presets.description_markup(form.get("description", "")),
        )
        await run_in_threadpool(store.save, user.user_id, preset_id, updated)
    except (PresetProblem, LookupError) as exc:
        return render(request, "_error.html", message=f"Not saved: {exc}")
    return go(request, f"/presets/{preset_id}/edit")


@router.post("/presets/{preset_id}/edit/{tab_id}")
async def preset_edit_tab(preset_id: str, tab_id: str, request: Request) -> Response:
    user = page_session(request)
    form = await read_form(
        request,
        MAX_PICKUP_FORM_BYTES
        if tab_id in ("pickups", "locations", "start", "transporters")
        else MAX_FORM_BYTES,
    )
    store = request.app.state.presets
    found = rdv_presets.tab(tab_id)
    if found is None or not found.ready:
        return render(request, "_error.html", status_code=404, message="Settings tab not found")
    preset = _editable(request, user, preset_id)
    try:
        changed = rdv_presets.apply(preset, tab_id, form)
        await run_in_threadpool(store.save, user.user_id, preset_id, changed)
    except PresetProblem as exc:
        return render(
            request, "_preset_tab.html", preset_id=preset_id, error=str(exc),
            **_tab_context(preset, found),
        )  # fmt: skip
    if not _is_htmx(request):
        return go(request, f"/presets/{preset_id}/edit?tab={tab_id}")
    return render(
        request, "_preset_tab.html", preset_id=preset_id, saved=True,
        **_tab_context(changed, found),
    )  # fmt: skip


@router.post("/presets/{preset_id}/delete")
def preset_delete(preset_id: str, request: Request) -> Response:
    user = page_session(request)
    request.app.state.presets.delete(user.user_id, preset_id)
    return go(request, "/presets")
