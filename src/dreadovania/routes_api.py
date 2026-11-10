# SPDX-License-Identifier: GPL-3.0-or-later
"""The generation API: presets, seeds and jobs (ADR 0011 §3).

Every route here requires a session (the router's dependency), and every
lookup is owner-scoped: someone else's preset, seed or job reads as 404.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from dreadovania.jobs import CannotPatch, CannotSend, JobQueue, QueueFull
from dreadovania.mods import ModError, iter_mod_tar, iter_mod_zip, mod_manifest, tar_size
from dreadovania.presets import MAX_PRESET_BYTES, PresetInfo, PresetStore
from dreadovania.problems import ApiProblem
from dreadovania.romfs import SUPPORTED_VERSIONS, RomfsStatus, State
from dreadovania.romfs_upload import (
    OffsetMismatch,
    RomfsUploads,
    UploadError,
    UploadNotFound,
    UploadStatus,
)
from dreadovania.routes_auth import API_PREFIX, CurrentUser, require_user
from dreadovania.seeds import NoSpoiler, SeedBusy, SeedNotReady, SeedStore, is_uuid
from dreadovania.switches import SwitchError, SwitchStore
from dreadovania.validation import InvalidInput

router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_user)])
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
SSE_KEEPALIVE_SECONDS = 15.0


def _presets(request: Request) -> PresetStore:
    return request.app.state.presets


def _seeds(request: Request) -> SeedStore:
    return request.app.state.seeds


def _jobs(request: Request) -> JobQueue:
    return request.app.state.jobs


def _not_found(what: str) -> ApiProblem:
    return ApiProblem(404, f"{what} not found")


# ---- models -------------------------------------------------------------------------------


class PresetOut(BaseModel):
    id: str
    name: str
    description: str
    builtin: bool

    @classmethod
    def of(cls, p: PresetInfo) -> PresetOut:
        return cls(id=p.id, name=p.name, description=p.description, builtin=p.builtin)


class GenerateIn(BaseModel):
    preset_id: str | None = Field(default=None, max_length=64)
    permalink: str | None = Field(default=None, max_length=4100)
    seed_number: int | None = None
    race: bool = False


class ModOut(BaseModel):
    """A seed's patched files, for Atmosphere (ADR 0013 §3)."""

    status: Literal["pending", "ready", "failed", "cancelled"]
    game_version: str | None
    file_count: int | None
    size_bytes: int | None
    randovania_version: str
    created_at: str
    completed_at: str | None
    job_id: str | None

    @classmethod
    def of(cls, row: dict[str, Any]) -> ModOut | None:
        if row.get("mod_status") is None:
            return None
        return cls(**{name: row[f"mod_{name}"] for name in cls.model_fields})


class SeedOut(BaseModel):
    id: str
    status: Literal["pending", "ready", "failed", "cancelled"]
    source: Literal["preset", "permalink"]
    preset_id: str | None
    preset_name: str | None
    requested_permalink: str | None
    seed_number: int | None
    race: bool
    permalink: str | None
    seed_hash: str | None
    randovania_version: str
    created_at: str
    completed_at: str | None
    job_id: str | None = Field(description="The job that generated this seed.")
    mod: ModOut | None = Field(description="The patched files, once the seed has been patched.")
    push_job_id: str | None = Field(
        default=None, description="The latest job that sent this seed to a Switch."
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> SeedOut:
        return cls(
            **{**row, "preset_id": row["preset_key"], "race": bool(row["race"])},
            mod=ModOut.of(row),
        )


class Submitted(BaseModel):
    seed_id: str
    job_id: str


class JobOut(BaseModel):
    id: str
    seed_id: str
    kind: Literal["generate", "export", "push"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: str | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def of(cls, row: dict[str, Any]) -> JobOut:
        return cls(**{k: row[k] for k in cls.model_fields})


class PurgeAll(BaseModel):
    scope: Literal["all"]


class PurgeOlderThan(BaseModel):
    scope: Literal["older_than"]
    days: int = Field(ge=1, le=3650)


class Purged(BaseModel):
    deleted: int
    skipped_in_progress: int


class SpoilerLocation(BaseModel):
    region: str
    area: str
    location: str
    pickup: str


class StartingLocation(BaseModel):
    region: str
    area: str
    node: str


class Spoiler(BaseModel):
    starting_location: StartingLocation | None
    starting_items: list[str]
    locations: list[SpoilerLocation]


class RomfsOut(BaseModel):
    """The game files' status. Never their contents, paths or file names."""

    state: State
    ready: bool
    message: str
    version: str | None
    supported_versions: list[str]
    file_count: int | None
    total_bytes: int | None
    upload_allowed: bool
    upload_blocked_reason: str | None = Field(
        description="Why game files can't be uploaded here (for example, the folder is a mount)."
    )

    @classmethod
    def of(cls, s: RomfsStatus, blocked: str | None) -> RomfsOut:
        return cls(
            upload_allowed=blocked is None,
            upload_blocked_reason=blocked,
            state=s.state,
            ready=s.ready,
            message=s.message,
            version=s.version,
            supported_versions=list(SUPPORTED_VERSIONS),
            file_count=s.file_count,
            total_bytes=s.total_bytes,
        )


# ---- game files -----------------------------------------------------------------------------


@router.get("/romfs", tags=["game files"])
async def get_romfs(request: Request, user: CurrentUser) -> RomfsOut:
    """Whether the server has usable game files (an extracted RomFS), and which version."""
    status = await run_in_threadpool(request.app.state.romfs.status)
    return RomfsOut.of(status, await run_in_threadpool(_uploads(request).unavailable_reason))


# ---- uploading game files (ADR 0013 §1) -------------------------------------------------------


class UploadStart(BaseModel):
    size: int = Field(gt=0, description="The archive's size in bytes.")
    fingerprint: str = Field(
        min_length=1,
        max_length=512,
        description="Identifies the file so an interrupted upload can be resumed "
        "(for example its name, size and modification time). Stored only as a hash.",
    )


class UploadOut(BaseModel):
    id: str
    state: Literal["receiving", "received", "installing", "failed", "installed"]
    size: int
    offset: int = Field(description="Bytes received so far: send the next chunk from here.")
    chunk_size: int = Field(description="The largest chunk the server accepts.")
    message: str | None
    installed_bytes: int
    total_bytes: int | None

    @classmethod
    def of(cls, s: UploadStatus) -> UploadOut:
        return cls(**asdict(s))


def _uploads(request: Request) -> RomfsUploads:
    return request.app.state.romfs_uploads


def _upload_problem(exc: UploadError) -> ApiProblem:
    if isinstance(exc, UploadNotFound):
        return _not_found("Upload")
    if isinstance(exc, OffsetMismatch):
        return ApiProblem(
            409, "Wrong offset", str(exc), {"Upload-Offset": str(exc.expected)},
            offset=exc.expected,
        )  # fmt: skip
    return ApiProblem(409, "Upload refused", str(exc))


@router.get("/romfs/upload", tags=["game files"])
def get_upload(request: Request, user: CurrentUser) -> UploadOut:
    """The current (or last) game files upload."""
    current = _uploads(request).current()
    if current is None:
        raise _not_found("Upload")
    return UploadOut.of(current)


@router.post(
    "/romfs/upload",
    tags=["game files"],
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": UploadOut, "description": "Resuming the same file."}},
)
async def start_upload(
    body: UploadStart, request: Request, response: Response, user: CurrentUser
) -> UploadOut:
    """Start uploading an archive (zip or tar) of an extracted RomFS, or resume the same file.

    Then send it in chunks with `PATCH`, and install it with `POST …/install`.
    """
    try:
        upload, created = await _uploads(request).start(body.size, body.fingerprint)
    except UploadError as exc:
        raise _upload_problem(exc) from None
    if not created:
        response.status_code = status.HTTP_200_OK
    return UploadOut.of(upload)


@router.patch(
    "/romfs/upload/{upload_id}",
    tags=["game files"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/offset+octet-stream": {"schema": {"type": "string",
                                                                       "format": "binary"}}},
        },
        "parameters": [{"name": "Upload-Offset", "in": "header", "required": True,
                        "schema": {"type": "integer", "minimum": 0},
                        "description": "Where this chunk starts: the upload's current offset."}],
    },
)  # fmt: skip
async def upload_chunk(upload_id: str, request: Request, user: CurrentUser) -> UploadOut:
    """Append one chunk (at most `chunk_size` bytes) at `Upload-Offset`.

    A 409 with `offset` means the server has a different amount: continue from there.
    """
    raw_offset = request.headers.get("upload-offset", "")
    if not raw_offset.isdigit():
        raise ApiProblem(400, "Upload-Offset required", "send the chunk's starting byte")
    try:
        upload = await _uploads(request).write(upload_id, int(raw_offset), request.stream())
    except UploadError as exc:
        raise _upload_problem(exc) from None
    return UploadOut.of(upload)


@router.post(
    "/romfs/upload/{upload_id}/install",
    tags=["game files"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def install_upload(upload_id: str, request: Request, user: CurrentUser) -> UploadOut:
    """Check, extract and validate the uploaded archive, then replace the game files with it.

    Runs in the background: follow it with `GET /romfs/upload`. The current game
    files are only replaced if the new ones are a supported, unmodified RomFS.
    """
    try:
        upload = await _uploads(request).install(upload_id)
    except UploadError as exc:
        raise _upload_problem(exc) from None
    return UploadOut.of(upload)


@router.delete(
    "/romfs/upload/{upload_id}", tags=["game files"], status_code=status.HTTP_204_NO_CONTENT
)
async def discard_upload(upload_id: str, request: Request, user: CurrentUser) -> None:
    """Discard an upload and the data received so far."""
    try:
        await _uploads(request).discard(upload_id)
    except UploadError as exc:
        raise _upload_problem(exc) from None


# ---- presets ------------------------------------------------------------------------------


@router.get("/presets", tags=["presets"])
def list_presets(request: Request, user: CurrentUser) -> list[PresetOut]:
    """Randovania's bundled Dread presets, then the user's imported ones."""
    return [PresetOut.of(p) for p in _presets(request).list(user.user_id)]


@router.post("/presets", status_code=status.HTTP_201_CREATED, tags=["presets"])
async def import_preset(request: Request, user: CurrentUser) -> PresetOut:
    """Import a `.rdvpreset` file. Send the file's JSON as the request body (max 1 MB)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_PRESET_BYTES:
        raise ApiProblem(413, "Preset too large", "presets can be at most 1 MB")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_PRESET_BYTES:
            raise ApiProblem(413, "Preset too large", "presets can be at most 1 MB")
    try:
        info = await run_in_threadpool(_presets(request).import_preset, user.user_id, bytes(raw))
    except InvalidInput as exc:
        raise ApiProblem(400, "Invalid preset", str(exc)) from None
    return PresetOut.of(info)


@router.get("/presets/{preset_id}", tags=["presets"])
def get_preset(preset_id: str, request: Request, user: CurrentUser) -> PresetOut:
    info = _presets(request).get(user.user_id, preset_id)
    if info is None:
        raise _not_found("Preset")
    return PresetOut.of(info)


@router.delete("/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["presets"])
def delete_preset(preset_id: str, request: Request, user: CurrentUser) -> None:
    """Delete an imported preset. Bundled presets can't be deleted."""
    if not _presets(request).delete(user.user_id, preset_id):
        raise _not_found("Preset")


# ---- seeds --------------------------------------------------------------------------------


@router.post("/seeds", status_code=status.HTTP_202_ACCEPTED, tags=["seeds"])
def generate_seed(body: GenerateIn, request: Request, user: CurrentUser) -> Submitted:
    """Start generating a seed from a preset or a permalink. Follow the job for progress."""
    try:
        sub = _jobs(request).submit_generate(
            user.user_id,
            preset_key=body.preset_id,
            permalink=body.permalink,
            seed_number=body.seed_number,
            race=body.race,
        )
    except InvalidInput as exc:
        raise ApiProblem(400, "Invalid request", str(exc)) from None
    except QueueFull as exc:
        raise ApiProblem(429, "Too many seeds waiting", str(exc)) from None
    return Submitted(seed_id=sub.seed_id, job_id=sub.job_id)


@router.get("/seeds", tags=["seeds"])
def list_seeds(request: Request, user: CurrentUser, limit: int = 50) -> list[SeedOut]:
    """Newest first."""
    return [SeedOut.of(r) for r in _seeds(request).list(user.user_id, limit)]


@router.post("/seeds/purge", tags=["seeds"])
def purge_seeds(
    body: Annotated[PurgeAll | PurgeOlderThan, Field(discriminator="scope")],
    request: Request,
    user: CurrentUser,
) -> Purged:
    """Delete all seeds, or all older than N days. The scope must be explicit."""
    days = body.days if isinstance(body, PurgeOlderThan) else None
    deleted, skipped = _seeds(request).purge(user.user_id, days)
    return Purged(deleted=deleted, skipped_in_progress=skipped)


@router.get("/seeds/{seed_id}", tags=["seeds"])
def get_seed(seed_id: str, request: Request, user: CurrentUser) -> SeedOut:
    seed = _seeds(request).get(user.user_id, seed_id)
    if seed is None:
        raise _not_found("Seed")
    return SeedOut.of(seed)


@router.get(
    "/seeds/{seed_id}/rdvgame",
    tags=["seeds"],
    response_class=FileResponse,
    responses={200: {"content": {"application/json": {}}}},
)
def download_rdvgame(seed_id: str, request: Request, user: CurrentUser) -> FileResponse:
    """Download the `.rdvgame`. For non-race seeds, this file contains the spoiler."""
    try:
        path = _seeds(request).rdvgame_path(user.user_id, seed_id)
    except SeedNotReady:
        raise ApiProblem(409, "Seed not ready") from None
    if path is None:
        raise _not_found("Seed")
    seed = _seeds(request).get(user.user_id, seed_id) or {}
    name = f"dreadovania-{seed.get('seed_hash') or seed_id}.rdvgame"
    return FileResponse(path, media_type="application/json", filename=name)


@router.get("/seeds/{seed_id}/spoiler", tags=["seeds"])
def get_spoiler(seed_id: str, request: Request, user: CurrentUser) -> Spoiler:
    """Where every pickup is. A separate request, so the UI can require a reveal step."""
    try:
        spoiler = _seeds(request).spoiler(user.user_id, seed_id)
    except SeedNotReady:
        raise ApiProblem(409, "Seed not ready") from None
    except NoSpoiler:
        raise ApiProblem(409, "No spoiler", "race seeds are generated without a spoiler") from None
    if spoiler is None:
        raise _not_found("Seed")
    return Spoiler(**spoiler)


@router.delete("/seeds/{seed_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["seeds"])
def delete_seed(seed_id: str, request: Request, user: CurrentUser) -> None:
    try:
        deleted = _seeds(request).delete(user.user_id, seed_id)
    except SeedBusy:
        raise ApiProblem(409, "Seed in progress", "cancel its job first") from None
    if not deleted:
        raise _not_found("Seed")


# ---- patching (ADR 0013) --------------------------------------------------------------------


@router.post("/seeds/{seed_id}/mod", status_code=status.HTTP_202_ACCEPTED, tags=["patching"])
def patch_seed(seed_id: str, request: Request, user: CurrentUser) -> Submitted:
    """Patch a generated seed into the server's game files. Follow the job for progress.

    Needs ready game files (see `GET /romfs`). Race seeds can be patched: patching
    doesn't reveal the spoiler.
    """
    if not is_uuid(seed_id):
        raise _not_found("Seed")
    try:
        sub = _jobs(request).submit_export(user.user_id, seed_id)
    except LookupError:
        raise _not_found("Seed") from None
    except CannotPatch as exc:
        raise ApiProblem(409, "Can't patch this seed", str(exc)) from None
    except QueueFull as exc:
        raise ApiProblem(429, "Too many jobs waiting", str(exc)) from None
    return Submitted(seed_id=sub.seed_id, job_id=sub.job_id)


@router.get(
    "/seeds/{seed_id}/mod.zip",
    tags=["patching"],
    response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {}}}},
)
def download_mod(seed_id: str, request: Request, user: CurrentUser) -> Response:
    """Download the patched seed as a zip. Extract it at the root of the SD card.

    It contains `atmosphere/…`, made from your own game files: don't share it.
    """
    seeds = _seeds(request)
    seed = seeds.get(user.user_id, seed_id)
    if seed is None:
        raise _not_found("Seed")
    folder = seeds.mod_dir(user.user_id, seed_id)
    if folder is None:
        raise ApiProblem(409, "Not patched", "patch this seed first")
    label = re.sub(r"[^A-Za-z0-9_-]", "", seed["seed_hash"] or "") or seed["id"]
    return StreamingResponse(
        iterate_in_threadpool(iter_mod_zip(folder)),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="dreadovania-{label}.zip"',
            "Cache-Control": "no-store",
        },
    )


class ModFile(BaseModel):
    path: str = Field(description="Where it goes on the SD card, e.g. atmosphere/contents/…")
    size: int
    sha256: str


class ModManifest(BaseModel):
    """The files in /mod.tar, in the same order. Check each one before installing."""

    seed_id: str
    file_count: int
    size_bytes: int
    tar_bytes: int = Field(description="The exact length of /mod.tar")
    files: list[ModFile]


def _patched_folder(request: Request, user_id: str, seed_id: str) -> tuple[dict, Any]:
    seeds = _seeds(request)
    seed = seeds.get(user_id, seed_id)
    if seed is None:
        raise _not_found("Seed")
    folder = seeds.mod_dir(user_id, seed_id)
    if folder is None:
        raise ApiProblem(409, "Not patched", "patch this seed first")
    return seed, folder


@router.get("/seeds/{seed_id}/mod/manifest", tags=["patching"])
async def mod_files(seed_id: str, request: Request, user: CurrentUser) -> ModManifest:
    """The patched seed's files with their sizes and SHA-256, for the Switch app (ADR 0018)."""
    _seed, folder = _patched_folder(request, user.user_id, seed_id)
    try:
        files = await run_in_threadpool(mod_manifest, folder)
        total = await run_in_threadpool(tar_size, folder)
    except ModError as exc:
        raise ApiProblem(500, "Patched files unreadable", str(exc)) from None
    return ModManifest(
        seed_id=seed_id, file_count=len(files), size_bytes=sum(int(f["size"]) for f in files),
        tar_bytes=total, files=[ModFile(**f) for f in files],  # type: ignore[arg-type]
    )  # fmt: skip


@router.get(
    "/seeds/{seed_id}/mod.tar",
    tags=["patching"],
    response_class=StreamingResponse,
    responses={200: {"content": {"application/x-tar": {}}}},
)
def download_mod_tar(seed_id: str, request: Request, user: CurrentUser) -> Response:
    """The patched seed as one uncompressed ustar archive, for the Switch app (ADR 0018).

    Members are `atmosphere/…`, in the order /mod/manifest lists them.
    """
    _seed, folder = _patched_folder(request, user.user_id, seed_id)
    return StreamingResponse(
        iterate_in_threadpool(iter_mod_tar(folder)),
        media_type="application/x-tar",
        headers={"Content-Length": str(tar_size(folder)), "Cache-Control": "no-store"},
    )


@router.delete("/seeds/{seed_id}/mod", status_code=status.HTTP_204_NO_CONTENT, tags=["patching"])
def delete_mod(seed_id: str, request: Request, user: CurrentUser) -> None:
    """Delete a seed's patched files to free space. The seed itself is kept."""
    try:
        deleted = _seeds(request).delete_mod(user.user_id, seed_id)
    except SeedBusy:
        raise ApiProblem(409, "Patching in progress", "cancel its job first") from None
    if not deleted:
        raise _not_found("Patched files")


# ---- Switches (ADR 0016) ------------------------------------------------------------------


class SwitchIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    host: str = Field(
        min_length=1, max_length=253,
        description="The Switch's local-network address, as ftpd shows it (an IP or a local name).",
    )  # fmt: skip
    port: int = Field(default=5000, ge=1, le=65535, description="ftpd's port (5000 by default).")
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(
        default=None, max_length=128, description="Write-only: never returned."
    )


class SwitchUpdate(BaseModel):
    """Only the fields sent are changed. Send `password: null` (or "") to remove it."""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=128, description="Write-only.")


class SwitchOut(BaseModel):
    id: str
    name: str
    host: str
    port: int
    username: str | None
    has_password: bool
    last_test_at: str | None
    last_test_ok: bool | None
    last_test_message: str | None
    created_at: str
    updated_at: str
    installed_seed_id: str | None = Field(
        description="The seed Dreadovania last installed on this Switch, if any."
    )
    install_state: Literal["installing", "installed"] | None = Field(
        description="'installing' if the last send didn't finish: the next one cleans it up."
    )


class ConnectionTest(BaseModel):
    ok: bool
    message: str


def _switches(request: Request) -> SwitchStore:
    return request.app.state.switches


def _switch_problem(exc: SwitchError) -> ApiProblem:
    return ApiProblem(400, "Invalid Switch", str(exc))


@router.get("/switches", tags=["switches"])
def list_switches(request: Request, user: CurrentUser) -> list[SwitchOut]:
    """Your saved Switches. Passwords are never included."""
    return [SwitchOut(**s) for s in _switches(request).list(user.user_id)]


@router.post("/switches", status_code=status.HTTP_201_CREATED, tags=["switches"])
async def add_switch(body: SwitchIn, request: Request, user: CurrentUser) -> SwitchOut:
    """Save a Switch running ftpd on your local network (public addresses are refused)."""
    try:
        created = await run_in_threadpool(
            _switches(request).create, user.user_id, body.model_dump()
        )
    except SwitchError as exc:
        raise _switch_problem(exc) from None
    return SwitchOut(**created)


@router.get("/switches/{switch_id}", tags=["switches"])
def get_switch(switch_id: str, request: Request, user: CurrentUser) -> SwitchOut:
    found = _switches(request).get(user.user_id, switch_id) if is_uuid(switch_id) else None
    if found is None:
        raise _not_found("Switch")
    return SwitchOut(**found)


@router.patch("/switches/{switch_id}", tags=["switches"])
async def update_switch(
    switch_id: str, body: SwitchUpdate, request: Request, user: CurrentUser
) -> SwitchOut:
    """Change a Switch. Fields left out are kept, including the password."""
    if not is_uuid(switch_id):
        raise _not_found("Switch")
    fields = body.model_dump(include=body.model_fields_set)
    try:
        updated = await run_in_threadpool(
            _switches(request).update, user.user_id, switch_id, fields
        )
    except SwitchError as exc:
        raise _switch_problem(exc) from None
    if updated is None:
        raise _not_found("Switch")
    return SwitchOut(**updated)


@router.delete("/switches/{switch_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["switches"])
def delete_switch(switch_id: str, request: Request, user: CurrentUser) -> None:
    if not is_uuid(switch_id) or not _switches(request).delete(user.user_id, switch_id):
        raise _not_found("Switch")


@router.post("/switches/{switch_id}/test", tags=["switches"])
async def test_switch(switch_id: str, request: Request, user: CurrentUser) -> ConnectionTest:
    """Log in to ftpd on the Switch and check for the Atmosphere folder.

    Open ftpd on the Switch first. Takes up to about ten seconds if it doesn't answer.
    """
    result = (
        await run_in_threadpool(_switches(request).test, user.user_id, switch_id)
        if is_uuid(switch_id)
        else None
    )
    if result is None:
        raise _not_found("Switch")
    return ConnectionTest(ok=result.ok, message=result.message)


class PushIn(BaseModel):
    switch_id: str = Field(min_length=1, max_length=64)
    overwrite: bool = Field(
        default=False,
        description="Replace files that another mod installed, if the seed needs them.",
    )
    clear: bool = Field(
        default=False,
        description="Empty Dread's mod folders on the Switch first, whatever is in them.",
    )


@router.post("/seeds/{seed_id}/push", status_code=status.HTTP_202_ACCEPTED, tags=["switches"])
def send_to_switch(seed_id: str, body: PushIn, request: Request, user: CurrentUser) -> Submitted:
    """Install a patched seed on a saved Switch over FTP. Open ftpd on the Switch first.

    The previous seed's files are removed first; other mods are kept. If the seed
    would replace another mod's files, the job fails and lists them, unless
    `overwrite` is set. Follow the job for progress.
    """
    if not is_uuid(seed_id) or not is_uuid(body.switch_id):
        raise _not_found("Seed or Switch")
    try:
        sub = _jobs(request).submit_push(
            user.user_id, seed_id, body.switch_id, overwrite=body.overwrite, clear=body.clear
        )
    except LookupError:
        raise _not_found("Seed") from None
    except CannotSend as exc:
        raise ApiProblem(409, "Can't send this seed", str(exc)) from None
    except QueueFull as exc:
        raise ApiProblem(429, "Too many jobs waiting", str(exc)) from None
    return Submitted(seed_id=sub.seed_id, job_id=sub.job_id)


# ---- jobs ---------------------------------------------------------------------------------


@router.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: str, request: Request, user: CurrentUser) -> JobOut:
    job = _jobs(request).get_job(job_id, user.user_id) if is_uuid(job_id) else None
    if job is None:
        raise _not_found("Job")
    return JobOut.of(job)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["jobs"])
def cancel_job(job_id: str, request: Request, user: CurrentUser) -> None:
    """Cancel a queued or running job."""
    queue = _jobs(request)
    if not is_uuid(job_id) or queue.get_job(job_id, user.user_id) is None:
        raise _not_found("Job")
    if not queue.cancel(job_id, user.user_id):
        raise ApiProblem(409, "Job already finished")


@router.get(
    "/jobs/{job_id}/events",
    tags=["jobs"],
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def job_events(job_id: str, request: Request, user: CurrentUser) -> Response:
    """Live progress as Server-Sent Events: one `job` event per change, until it finishes."""
    queue = _jobs(request)
    owner = user.user_id
    if not is_uuid(job_id) or await run_in_threadpool(queue.get_job, job_id, owner) is None:
        raise _not_found("Job")

    async def stream() -> AsyncIterator[str]:
        version, last = queue.version, None
        while not await request.is_disconnected():
            job = await run_in_threadpool(queue.get_job, job_id, owner)
            if job is None:
                return
            payload = JobOut.of(job).model_dump_json()
            if payload != last:
                last = payload
                yield f"event: job\ndata: {payload}\n\n"
            if job["status"] in TERMINAL:
                return
            new_version = await queue.wait_for_change(version, SSE_KEEPALIVE_SECONDS)
            if new_version == version:
                yield ": keepalive\n\n"
            version = new_version

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
