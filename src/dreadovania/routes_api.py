# SPDX-License-Identifier: GPL-3.0-or-later
"""The generation API: presets, seeds and jobs (ADR 0011 §3).

Every route here requires a session (the router's dependency), and every
lookup is owner-scoped: someone else's preset, seed or job reads as 404.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from dreadovania.jobs import JobQueue, QueueFull
from dreadovania.presets import MAX_PRESET_BYTES, PresetInfo, PresetStore
from dreadovania.problems import ApiProblem
from dreadovania.romfs import SUPPORTED_VERSIONS, RomfsStatus, State
from dreadovania.routes_auth import API_PREFIX, CurrentUser, require_user
from dreadovania.seeds import NoSpoiler, SeedBusy, SeedNotReady, SeedStore, is_uuid
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
    job_id: str | None

    @classmethod
    def of(cls, row: dict[str, Any]) -> SeedOut:
        return cls(**{**row, "preset_id": row["preset_key"], "race": bool(row["race"])})


class Submitted(BaseModel):
    seed_id: str
    job_id: str


class JobOut(BaseModel):
    id: str
    seed_id: str
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

    @classmethod
    def of(cls, s: RomfsStatus) -> RomfsOut:
        return cls(
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
    return RomfsOut.of(await run_in_threadpool(request.app.state.romfs.status))


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
