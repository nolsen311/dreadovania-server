# SPDX-License-Identifier: GPL-3.0-or-later
"""The mobile web UI: server-rendered Jinja pages with htmx (ADR 0011, PR 4).

These routes are not part of the API contract (they're excluded from OpenAPI).
They reuse the same stores and the same sign-in flows as the API, so the same
rules apply: sessions, Origin checks on every change, rate limits, owner scoping.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from dreadovania import (
    __version__,
    accounts,
    auth,
    client_certs,
    devices,
    passkeys,
    rdv_presets,
    remote_access,
)
from dreadovania import cosmetics as cosmetic_options
from dreadovania.jobs import CannotPatch, CannotSend, QueueFull
from dreadovania.problems import ApiProblem
from dreadovania.rdv_presets import PresetProblem
from dreadovania.romfs import SUPPORTED_VERSIONS
from dreadovania.routes_auth import (
    SAFE_METHODS,
    change_remote_access,
    check_origin,
    front_door_origin,
    invite_certificate,
    invite_url,
    main_origin,
    on_front_door,
    perform_accept_invite,
    perform_login,
    perform_logout,
    perform_setup,
    presented_certificate,
    public_origin,
    remote_access_status,
    uses_front_door,
)
from dreadovania.seeds import NoSpoiler, SeedBusy, SeedNotReady
from dreadovania.switches import DEFAULT_PORT, SwitchError
from dreadovania.validation import InvalidInput

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["preset_text"] = rdv_presets.description_text
# In static URLs, so a new release never runs with an old cached script.
templates.env.globals["version"] = __version__
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
    if context.get("user") is not None and "certificate_expires_in" not in context:
        context["certificate_expires_in"] = _expiring(request)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _expiring(request: Request) -> int | None:
    """Days left on this browser's certificate, for the banner (ADR 0023 amendment)."""
    if getattr(request.app.state, "remote_access", None) is None:
        return None
    cert = presented_certificate(request)
    if cert is None:
        return None
    # A replacement in use: revoke the certificate it replaced (ADR 0023 amendment).
    request.app.state.client_certs.confirm_renewal(cert["owner_id"], cert["id"])
    return remote_access.expiring_soon(cert) if cert["kind"] == "browser" else None


def install(app: Any) -> None:
    from fastapi.staticfiles import StaticFiles

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect) -> Response:
        return go(request, exc.url)

    app.state.certificate_downloads = OneTimeDownloads()
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
            context["romfs"] = request.app.state.game_files.inspector(user.user_id).status()
        if seed["mod_status"] == "ready":
            context.update(_push_context(request, user, seed))
            current = request.app.state.jobs.cosmetics.get(user.user_id)
            context["cosmetics_current"] = (
                cosmetic_options.recorded(seed.get("mod_cosmetics")) == current
            )
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
    game_files = request.app.state.game_files  # the account's own (ADR 0020 §3)
    status = await run_in_threadpool(game_files.inspector(user.user_id).status)
    uploads = game_files.uploads(user.user_id)
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


def _cosmetics_page(request: Request, user: auth.Session, c: Any, status_code: int = 200,
                    error: str | None = None) -> Response:  # fmt: skip
    return render(
        request, "cosmetics.html", status_code=status_code, user=user, c=c, error=error,
        hud=cosmetic_options.HUD, room_names=cosmetic_options.ROOM_NAMES,
        missile_colours=cosmetic_options.MISSILE_COLOURS, volumes=cosmetic_options.VOLUMES,
        shields=cosmetic_options.SHIELDS,
    )  # fmt: skip


@router.get("/account/cosmetics")
def cosmetics_page(request: Request) -> Response:
    user = page_session(request)
    return _cosmetics_page(request, user, request.app.state.jobs.cosmetics.get(user.user_id))


@router.post("/account/cosmetics")
async def cosmetics_save(request: Request) -> Response:
    """The Cosmetics form (ADR 0025). An unticked checkbox isn't sent, so it means off."""
    user = page_session(request)
    form = await read_form(request)
    fields: dict[str, Any] = {
        name: form.get(name) == "on"
        for name in ["separate_save_slots", *(n for n, _ in cosmetic_options.HUD)]
    }
    fields |= {
        name: "ALTERNATE" if form.get(name) == "on" else "DEFAULT"
        for name, _ in cosmetic_options.SHIELDS
    }
    fields |= {name: form.get(name, "").strip() for name, _ in cosmetic_options.VOLUMES}
    fields |= {name: form.get(name, "") for name in ("show_room_names", "missile_cosmetic")}
    try:
        options = cosmetic_options.Cosmetics.model_validate(fields)
    except ValidationError:
        return _cosmetics_page(
            request, user, request.app.state.jobs.cosmetics.get(user.user_id), status_code=400,
            error="Volumes are whole numbers from 0 to 100; pick the rest from the lists.",
        )  # fmt: skip
    request.app.state.jobs.cosmetics.set(user.user_id, options)
    return RedirectResponse("/account/cosmetics?saved=1", status_code=303)


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
    db = request.app.state.db
    return render(
        request, "account.html", user=user,
        passkeys=passkeys.list_passkeys(db, user.user_id),
        passkeys_unavailable=_passkeys_unavailable(request),
        devices=devices.list_devices(db, user.user_id),
        certificates=request.app.state.client_certs.list(user.user_id),
        presented=presented_certificate(request),
        server_url=public_origin(request),
        accounts=[
            a | {"game_files_bytes": request.app.state.game_files.size(a["id"])}
            for a in accounts.list_accounts(db, user.user_id)
        ]
        if user.admin
        else [],
        invites=accounts.list_invites(db, user.user_id) if user.admin else [],
    )  # fmt: skip


# ---- client certificates (ADR 0023) ----------------------------------------------------------

DOWNLOAD_LIFETIME = 10 * 60  # seconds


class OneTimeDownloads:
    """A new certificate's .p12, kept in memory until it's downloaded once, for a few
    minutes at most. The server never writes it to disk (ADR 0023 §2). For an invite,
    its password waits here too, to be shown once on the welcome page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def add(self, owner_id: str, p12: bytes, name: str, password: str | None = None) -> str:
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v["expires"] > now}
            self._items[token] = {"expires": now + DOWNLOAD_LIFETIME, "owner": owner_id,
                                  "p12": p12, "name": name, "password": password}  # fmt: skip
        return token

    def _live(self, owner_id: str, token: str) -> dict[str, Any] | None:
        item = self._items.get(token)
        if item is None or item["owner"] != owner_id or item["expires"] <= time.monotonic():
            return None
        return item

    def take(self, owner_id: str, token: str) -> tuple[bytes, str] | None:
        with self._lock:
            item = self._live(owner_id, token)
            if item is None:
                return None
            del self._items[token]
        return item["p12"], item["name"]

    def take_password(self, owner_id: str, token: str) -> str | None:
        """The password, once; the download stays available."""
        with self._lock:
            item = self._live(owner_id, token)
            if item is None:
                return None
            password, item["password"] = item["password"], None
        return password


def _filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")[:40]
    return f"dreadovania-{safe or 'certificate'}.p12"


@router.post("/account/certificates")
async def certificate_create(request: Request) -> Response:
    user = page_session(request)
    form = await read_form(request)
    certs: client_certs.ClientCertificates = request.app.state.client_certs
    try:
        issued = certs.issue(user.user_id, "browser", form.get("name", ""))
    except client_certs.CertificateError as exc:
        return render(request, "_error.html", message=str(exc))
    token = request.app.state.certificate_downloads.add(
        user.user_id, issued.p12, issued.certificate["name"]
    )
    return render(
        request, "_certificate_issued.html", certificate=issued.certificate,
        password=issued.password, token=token, minutes=DOWNLOAD_LIFETIME // 60,
    )  # fmt: skip


@router.post("/account/certificates/replace")
def certificate_replace(request: Request) -> Response:
    """A replacement for the certificate this browser is using, from the expiry banner.
    The old one keeps working until the browser connects with the new one."""
    user = page_session(request)
    cert = presented_certificate(request)
    if cert is None or cert["owner_id"] != user.user_id or cert["kind"] != "browser":
        return render(
            request, "_error.html", message="this browser isn't using one of your certificates"
        )
    certs: client_certs.ClientCertificates = request.app.state.client_certs
    try:
        issued = certs.renew(user.user_id, cert["id"])
    except client_certs.CertificateError as exc:
        return render(request, "_error.html", message=str(exc))
    token = request.app.state.certificate_downloads.add(
        user.user_id, issued.p12, issued.certificate["name"]
    )
    return render(
        request, "_certificate_issued.html", certificate=issued.certificate,
        password=issued.password, token=token, minutes=DOWNLOAD_LIFETIME // 60, replacing=cert,
    )  # fmt: skip


@router.post("/account/certificates/external")
async def certificate_external_fallback(request: Request) -> Response:
    """The upload form without its script, which sends it to the API. Nothing it sent is
    read or kept: the page only asks for a reload, which brings the current script."""
    page_session(request)
    return render(
        request, "_error.html", status_code=400,
        message="this page's script didn't run, so nothing was uploaded: reload the page"
        " (Ctrl+Shift+R, or Cmd+Shift+R on a Mac) and try again",
    )  # fmt: skip


@router.get("/account/certificates/download/{token}")
def certificate_download(token: str, request: Request) -> Response:
    user = page_session(request)
    item = request.app.state.certificate_downloads.take(user.user_id, token)
    if item is None:
        return render(
            request, "_error.html", status_code=404,
            message="that download has been used or has expired: add a new certificate",
        )  # fmt: skip
    p12, name = item
    return Response(
        p12, media_type="application/x-pkcs12",
        headers={"Content-Disposition": f'attachment; filename="{_filename(name)}"',
                 "Cache-Control": "no-store"},
    )  # fmt: skip


@router.post("/account/certificates/{certificate_id}/revoke")
def certificate_revoke(certificate_id: str, request: Request) -> Response:
    user = page_session(request)
    request.app.state.client_certs.revoke(user.user_id, certificate_id)
    return go(request, "/account")


@router.get("/account/remote-access")
def remote_access_page(request: Request) -> Response:
    user = admin_session(request)
    everyone = accounts.list_accounts(request.app.state.db, user.user_id)
    missing = set(request.app.state.client_certs.without_browser_certificate(
        [a["id"] for a in everyone]
    ))  # fmt: skip
    return render(
        request, "remote_access.html", user=user, status=remote_access_status(request),
        without_certificate=[a["username"] for a in everyone if a["id"] in missing],
        external_ca=_external_ca(request),
    )  # fmt: skip


def _external_ca(request: Request) -> dict[str, str] | None:
    ca = request.app.state.client_certs.external_ca()
    if ca is None:
        return None
    return {"subject": ca.subject.rfc4514_string(), "not_after": ca.not_valid_after_utc.isoformat()}


@router.post("/account/remote-access")
async def remote_access_submit(request: Request) -> Response:
    user = admin_session(request)
    form = await read_form(request)
    action = form.get("action", "")
    mode = form.get("mode") or None
    check_forwarded = form.get("check_forwarded") == "on"
    state = request.app.state.remote_access.state()
    try:
        certs: client_certs.ClientCertificates = request.app.state.client_certs
        if action == "external_ca":
            try:
                certs.set_external_ca(form.get("ca", "").strip().encode())
            except client_certs.CertificateError as exc:
                return render(request, "_error.html", message=str(exc))
            return go(request, "/account/remote-access")
        if action == "external_ca_remove":
            certs.set_external_ca(None)
            return go(request, "/account/remote-access")
        if action == "choose" and state.enabled:
            return render(request, "_error.html", message="turn it off before changing who checks")
        if action == "choose":
            change_remote_access(request, user, False, mode,
                                 check_forwarded=check_forwarded, confirmed=False)  # fmt: skip
        elif action == "on":
            change_remote_access(
                request, user, True, state.mode, check_forwarded=state.check_forwarded,
                confirmed=form.get("confirmed") == "on",
            )  # fmt: skip
        elif action == "off":
            change_remote_access(
                request, user, False, state.mode, check_forwarded=state.check_forwarded,
                confirmed=False,
            )  # fmt: skip
        else:
            return render(request, "_error.html", message="unknown action")
    except ApiProblem as problem:
        return render(request, "_error.html", message=problem.detail or problem.title)
    return go(request, "/account/remote-access")


# ---- accounts and invites (ADR 0020) --------------------------------------------------------


def admin_session(request: Request) -> auth.Session:
    user = page_session(request)
    if not user.admin:
        raise ApiProblem(403, "Admin only", "only the server's admin can manage accounts")
    return user


@router.get("/invite")
def invite_page(request: Request) -> Response:
    if auth.setup_required(request.app.state.db):
        return go(request, "/setup")
    return render(request, "invite.html")


@router.post("/invite")
async def invite_submit(request: Request) -> Response:
    form = await read_form(request)
    # From the front door (ADR 0023), the rest of the server is at its main address.
    response = go(request, f"{main_origin(request) or ''}/" if on_front_door(request) else "/")
    try:
        perform_accept_invite(
            request, response, form.get("token", "")[:128], form.get("username", "")[:64],
            form.get("password", "")[: auth.MAX_PASSWORD_LENGTH],
        )  # fmt: skip
    except ApiProblem as problem:
        return render(request, "_error.html", message=problem.detail or problem.title)
    user_id = request.state.accepted_user_id
    issued = invite_certificate(request, user_id)
    if issued is None:
        return response
    # Secure remote access is on: the new member needs this browser's certificate before
    # anything past the front door answers. The welcome page is on the front door too.
    token = request.app.state.certificate_downloads.add(
        user_id, issued.p12, issued.certificate["name"], password=issued.password
    )
    welcome = go(request, f"/invite/welcome/{token}")
    for name, value in response.headers.items():
        if name == "set-cookie":
            welcome.headers.append(name, value)
    return welcome


def _invite_session(request: Request) -> auth.Session | None:
    """The member who just joined, by their new session cookie (no Origin check: GET)."""
    token = request.cookies.get("dreadovania_session")
    return auth.resolve_session(request.app.state.db, token) if token else None


@router.get("/invite/welcome/{token}")
def invite_welcome(token: str, request: Request) -> Response:
    user = _invite_session(request)
    password = (
        request.app.state.certificate_downloads.take_password(user.user_id, token) if user else None
    )
    if user is None or password is None:
        return render(
            request, "_error.html", status_code=404,
            message="this page has been shown already or has expired: ask the server's owner"
            " to add a certificate for you",
        )  # fmt: skip
    return render(
        request, "invite_welcome.html", token=token, password=password,
        minutes=DOWNLOAD_LIFETIME // 60, server_url=main_origin(request),
    )  # fmt: skip


@router.get("/invite/certificate/{token}")
def invite_certificate_download(token: str, request: Request) -> Response:
    user = _invite_session(request)
    item = request.app.state.certificate_downloads.take(user.user_id, token) if user else None
    if item is None:
        return render(
            request, "_error.html", status_code=404,
            message="that download has been used or has expired",
        )  # fmt: skip
    p12, name = item
    return Response(
        p12, media_type="application/x-pkcs12",
        headers={"Content-Disposition": f'attachment; filename="{_filename(name)}"',
                 "Cache-Control": "no-store"},
    )  # fmt: skip


@router.post("/account/invites")
async def invite_create(request: Request) -> Response:
    user = admin_session(request)
    form = await read_form(request)
    try:
        token, invite = accounts.create_invite(
            request.app.state.db, user.user_id, form.get("note", "")
        )
    except accounts.AccountError as exc:
        return render(request, "_error.html", message=str(exc))
    return render(
        request, "_invite_link.html", invite=invite, token=token, url=invite_url(request, token)
    )


@router.post("/account/invites/{invite_id}/revoke")
def invite_revoke(invite_id: str, request: Request) -> Response:
    user = admin_session(request)
    accounts.revoke_invite(request.app.state.db, user.user_id, invite_id)
    return go(request, "/account")


@router.post("/account/members/{account_id}/remove")
async def member_remove(account_id: str, request: Request) -> Response:
    user = admin_session(request)
    form = await read_form(request)
    db = request.app.state.db
    target = next(
        (a for a in accounts.list_accounts(db, user.user_id) if a["id"] == account_id), None
    )
    if target is None:
        return render(request, "_error.html", status_code=404, message="account not found")
    if form.get("confirm", "").strip() != target["username"]:
        return render(
            request, "_error.html", message=f"type {target['username']} exactly to confirm"
        )
    try:
        accounts.remove_account(
            db, request.app.state.settings.data_dir, user.user_id, account_id,
            request.app.state.game_files,
        )  # fmt: skip
    except accounts.AccountError as exc:
        return render(request, "_error.html", message=str(exc))
    return go(request, "/account")


@router.post("/account/devices/code")
def device_code(request: Request) -> Response:
    user = page_session(request)
    code, _expires = devices.new_pairing_code(request.app.state.db, user.user_id)
    door = uses_front_door(request)
    return render(
        request, "_pairing_code.html", code=code, minutes=int(devices.CODE_LIFETIME.seconds // 60),
        server_url=front_door_origin(request) if door else public_origin(request),
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
        "preset_name": preset.name,
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
def preset_edit_page(
    preset_id: str, request: Request, tab: str = "generation", created: bool = False
) -> Response:
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
        description=preset.description, created=created,
        **_tab_context(preset, found),
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


@router.post("/presets/{preset_id}/edit/{tab_id}/save-as")
async def preset_edit_save_as(preset_id: str, tab_id: str, request: Request) -> Response:
    """Every setting of this preset, with this tab's unsaved changes, as a new preset.

    The preset being edited is left as it was.
    """
    user = page_session(request)
    form = await read_form(request, MAX_PICKUP_FORM_BYTES)
    store = request.app.state.presets
    found = rdv_presets.tab(tab_id)
    if found is None or not found.ready:
        return render(request, "_error.html", status_code=404, message="Settings tab not found")
    preset = _editable(request, user, preset_id)
    name = form.get("save_as_name", "").strip() or f"{preset.name} (variant)"[:100]
    try:
        changed = rdv_presets.apply(preset, tab_id, form)
        info = await run_in_threadpool(
            store.create, user.user_id, rdv_presets.copy_of(changed, name)
        )
    except PresetProblem as exc:
        return render(
            request, "_preset_tab.html", preset_id=preset_id, error=str(exc),
            **_tab_context(preset, found),
        )  # fmt: skip
    return go(request, f"/presets/{info.id}/edit?tab={tab_id}&created=1")


@router.post("/presets/{preset_id}/delete")
def preset_delete(preset_id: str, request: Request) -> Response:
    user = page_session(request)
    request.app.state.presets.delete(user.user_id, preset_id)
    return go(request, "/presets")
