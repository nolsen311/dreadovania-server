# SPDX-License-Identifier: GPL-3.0-or-later
"""The persistent, single-worker job queue (ADR 0011 §2).

The queue is the ``jobs`` table, so queued work survives a restart. One worker
runs one Randovania process at a time. A job found ``running`` at startup was
interrupted; it is marked failed and never silently re-run.
"""

from __future__ import annotations

import asyncio
import contextlib
import ftplib
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dreadovania import switches, validation
from dreadovania.db import Database, row_to_dict, utc_now
from dreadovania.mods import ModError, finalize_mod
from dreadovania.presets import PresetStore, bundled_dread_presets, is_user_preset
from dreadovania.push import Progress, PushError, PushOptions, push
from dreadovania.romfs import RomfsInspector
from dreadovania.runner import ExportRequest, GenerateRequest, Runner, RunnerError
from dreadovania.switches import SwitchError

log = logging.getLogger(__name__)

INTERRUPTED = "interrupted: the server stopped while this job was running"


class QueueFull(Exception):
    pass


class CannotSend(Exception):
    """Sending to a Switch can't start. The message is safe to show to the user."""


class CannotPatch(Exception):
    """Patching can't start. The message is safe to show to the user."""


@dataclass(frozen=True)
class Submitted:
    seed_id: str
    job_id: str


_Update = tuple[str, tuple[Any, ...]]


def new_id() -> str:
    return str(uuid.uuid4())


class JobQueue:
    def __init__(
        self,
        db: Database,
        runner: Runner,
        data_dir: Path,
        randovania_version: str,
        max_queued_per_owner: int = 5,
        progress_interval: float = 0.5,
        romfs: RomfsInspector | None = None,
    ):
        self.db = db
        self.runner = runner
        self.presets = PresetStore(db)
        self.seeds_dir = data_dir / "seeds"
        self.work_dir = data_dir / "work"
        self.randovania_version = randovania_version
        self.max_queued_per_owner = max_queued_per_owner
        self.progress_interval = progress_interval
        self.romfs = romfs
        # Resolves a Switch's address (tests replace it).
        self.switch_resolver: switches.Resolver = switches.resolve
        # True while uploaded game files are being installed (set by the app).
        self.romfs_busy: Callable[[], bool] = lambda: False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup = asyncio.Event()
        self._changed = asyncio.Condition()
        self._version = 0
        self._current: tuple[str, asyncio.Task[Any]] | None = None
        self._cancel_requested: set[str] = set()
        self._background: set[asyncio.Task[None]] = set()

    # ---- submitting (callable from any thread) -------------------------------

    def submit_generate(
        self,
        owner_id: str,
        *,
        preset_key: str | None = None,
        permalink: str | None = None,
        seed_number: int | None = None,
        race: bool = False,
    ) -> Submitted:
        if (preset_key is None) == (permalink is None):
            raise validation.InvalidInput("choose either a preset or a permalink")
        if permalink is not None:
            permalink = validation.check_permalink(permalink)
            if seed_number is not None or race:
                raise validation.InvalidInput(
                    "a permalink already contains its seed number and race setting"
                )
        else:
            preset = self.presets.get(owner_id, preset_key)
            if preset is None:
                raise validation.InvalidInput("unknown preset")
            preset_name = preset.name
            seed_number = validation.check_seed_number(
                seed_number if seed_number is not None
                else secrets.randbelow(validation.MAX_SEED_NUMBER)
            )  # fmt: skip

        if permalink is not None:
            preset_name = None
        seed_id, job_id, now = new_id(), new_id(), utc_now()
        with self.db.transaction() as conn:
            self._check_queue_room(conn, owner_id)
            conn.execute(
                "INSERT INTO seeds (id, owner_id, status, source, preset_key, preset_name,"
                " requested_permalink, seed_number, race, randovania_version, created_at)"
                " VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                (seed_id, owner_id, "preset" if preset_key else "permalink", preset_key,
                 preset_name, permalink, seed_number, int(race), self.randovania_version, now),
            )  # fmt: skip
            conn.execute(
                "INSERT INTO jobs (id, owner_id, seed_id, kind, status, created_at)"
                " VALUES (?, ?, ?, 'generate', 'queued', ?)",
                (job_id, owner_id, seed_id, now),
            )
        self._notify_from_any_thread()
        return Submitted(seed_id=seed_id, job_id=job_id)

    def submit_export(self, owner_id: str, seed_id: str) -> Submitted:
        """Queue patching a ready seed into the user's game files (ADR 0013 §2)."""
        if self.romfs is None:
            raise CannotPatch("patching isn't available on this server")
        game_files = self.romfs.status()
        if not game_files.ready:
            raise CannotPatch(f"the game files aren't ready: {game_files.message}")
        job_id, now = new_id(), utc_now()
        with self.db.transaction() as conn:
            seed = conn.execute(
                "SELECT status FROM seeds WHERE id = ? AND owner_id = ?", (seed_id, owner_id)
            ).fetchone()
            if seed is None:
                raise LookupError(seed_id)
            if seed["status"] != "ready":
                raise CannotPatch("only a generated seed can be patched")
            mod = conn.execute("SELECT status FROM mods WHERE seed_id = ?", (seed_id,)).fetchone()
            if mod is not None and mod["status"] == "pending":
                raise CannotPatch("this seed is already being patched")
            if mod is not None and mod["status"] == "ready":
                raise CannotPatch("this seed is already patched")
            self._check_queue_room(conn, owner_id)
            conn.execute(
                "INSERT INTO mods (seed_id, owner_id, status, randovania_version, created_at)"
                " VALUES (?, ?, 'pending', ?, ?)"
                " ON CONFLICT (seed_id) DO UPDATE SET status = 'pending',"
                " game_version = NULL, file_count = NULL, size_bytes = NULL,"
                " randovania_version = excluded.randovania_version,"
                " created_at = excluded.created_at, completed_at = NULL",
                (seed_id, owner_id, self.randovania_version, now),
            )
            conn.execute(
                "INSERT INTO jobs (id, owner_id, seed_id, kind, status, created_at)"
                " VALUES (?, ?, ?, 'export', 'queued', ?)",
                (job_id, owner_id, seed_id, now),
            )
        self._notify_from_any_thread()
        return Submitted(seed_id=seed_id, job_id=job_id)

    def submit_push(
        self,
        owner_id: str,
        seed_id: str,
        switch_id: str,
        *,
        overwrite: bool = False,
        clear: bool = False,
    ) -> Submitted:
        """Queue sending a patched seed to a saved Switch (ADR 0016)."""
        job_id, now = new_id(), utc_now()
        with self.db.transaction() as conn:
            seed = conn.execute(
                "SELECT s.status, m.status AS mod_status FROM seeds s"
                " LEFT JOIN mods m ON m.seed_id = s.id WHERE s.id = ? AND s.owner_id = ?",
                (seed_id, owner_id),
            ).fetchone()
            if seed is None:
                raise LookupError(seed_id)
            if seed["mod_status"] != "ready":
                raise CannotSend("patch this seed for Switch first")
            switch = conn.execute(
                "SELECT 1 FROM switches WHERE id = ? AND owner_id = ?", (switch_id, owner_id)
            ).fetchone()
            if switch is None:
                raise CannotSend("that Switch isn't saved: add it on the Switches page")
            busy = conn.execute(
                "SELECT 1 FROM jobs WHERE kind = 'push' AND status IN ('queued', 'running')"
                " AND owner_id = ? AND json_extract(params, '$.switch_id') = ?",
                (owner_id, switch_id),
            ).fetchone()
            if busy is not None:
                raise CannotSend("a seed is already being sent to that Switch")
            self._check_queue_room(conn, owner_id)
            params = json.dumps({"switch_id": switch_id, "overwrite": overwrite, "clear": clear})
            conn.execute(
                "INSERT INTO jobs (id, owner_id, seed_id, kind, status, params, created_at)"
                " VALUES (?, ?, ?, 'push', 'queued', ?, ?)",
                (job_id, owner_id, seed_id, params, now),
            )
        self._notify_from_any_thread()
        return Submitted(seed_id=seed_id, job_id=job_id)

    def export_running(self) -> bool:
        """Whether a patch is reading the game files right now."""
        with self.db.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM jobs WHERE kind = 'export' AND status = 'running' LIMIT 1"
                ).fetchone()
                is not None
            )

    def _check_queue_room(self, conn: Any, owner_id: str) -> None:
        active = conn.execute(
            "SELECT count(*) FROM jobs WHERE owner_id = ? AND status IN ('queued', 'running')",
            (owner_id,),
        ).fetchone()[0]
        if active >= self.max_queued_per_owner:
            raise QueueFull(f"you already have {active} jobs waiting; try again when one finishes")

    def _notify_from_any_thread(self) -> None:
        loop = self._loop
        if loop is None:
            return  # the worker isn't running yet; it checks the queue when it starts
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wakeup.set()
            self._bump_soon()
        else:
            loop.call_soon_threadsafe(self._wakeup.set)
            loop.call_soon_threadsafe(self._bump_soon)

    # ---- reading --------------------------------------------------------------

    def get_job(self, job_id: str, owner_id: str) -> dict[str, Any] | None:
        """Owner-scoped: someone else's job reads as not found."""
        with self.db.connect() as conn:
            return row_to_dict(
                conn.execute(
                    "SELECT * FROM jobs WHERE id = ? AND owner_id = ?", (job_id, owner_id)
                ).fetchone()
            )

    async def wait_for_change(self, version: int, timeout: float) -> int:
        """Wait until anything in the queue changes; returns the new version.

        Used for live progress (SSE, PR 3): callers re-read their job afterwards.
        """
        async with self._changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: self._version != version), timeout
                )
            return self._version

    @property
    def version(self) -> int:
        return self._version

    def _bump_soon(self) -> None:
        """Schedule a change notification from the event loop thread.

        Keeps a reference to the task until it finishes, so it can't be
        garbage-collected before it runs.
        """
        task = asyncio.get_running_loop().create_task(self._bump())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _bump(self) -> None:
        async with self._changed:
            self._version += 1
            self._changed.notify_all()

    # ---- cancelling -------------------------------------------------------------

    def cancel(self, job_id: str, owner_id: str) -> bool:
        """Cancel a queued or running job. Returns False if there was nothing to cancel."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id, status, seed_id, kind FROM jobs WHERE id = ? AND owner_id = ?",
                (job_id, owner_id),
            ).fetchone()
            if row is None or row["status"] not in ("queued", "running"):
                return False
            if row["status"] == "queued":
                self._finish(conn, dict(row), "cancelled")
        if row["status"] == "running":
            self._cancel_requested.add(job_id)
            current = self._current
            if current is not None and current[0] == job_id and self._loop is not None:
                self._loop.call_soon_threadsafe(current[1].cancel)
        self._notify_from_any_thread()
        return True

    # ---- lifecycle ----------------------------------------------------------------

    def recover_interrupted(self) -> int:
        """Mark jobs left running by a previous process as failed. Call before starting."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, seed_id, kind FROM jobs WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                self._finish(conn, dict(row), "failed", INTERRUPTED)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        if rows:
            log.warning("marked %d interrupted job(s) as failed", len(rows))
        return len(rows)

    async def run_worker(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        while True:
            job = self._claim_next()
            if job is None:
                self._wakeup.clear()
                # Re-check after clearing, so a submit between the two can't be missed.
                job = self._claim_next()
                if job is None:
                    await self._wakeup.wait()
                    continue
            await self._run(job)

    def _claim_next(self) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT j.id, j.owner_id, j.seed_id, j.kind, j.params, s.preset_key,"
                " s.requested_permalink,"
                " s.seed_number, s.race FROM jobs j JOIN seeds s ON s.id = j.seed_id"
                " WHERE j.status = 'queued' ORDER BY j.created_at, j.rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                (utc_now(), row["id"]),
            )
            return dict(row)

    async def _run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        workdir = self.work_dir / job_id
        workdir.mkdir(parents=True)
        await self._bump()

        last_flush = 0.0
        latest: list[str] = []

        def on_progress(line: str) -> None:
            nonlocal last_flush
            latest[:] = [line]
            now = time.monotonic()
            if now - last_flush >= self.progress_interval:
                last_flush = now
                self._set_progress(job_id, line)
                self._bump_soon()

        work = {"generate": self._generate, "export": self._export, "push": self._push}[job["kind"]]
        task = asyncio.create_task(work(job, workdir, on_progress))
        self._current = (job_id, task)
        if job_id in self._cancel_requested:
            task.cancel()  # cancelled between being claimed and starting
        try:
            sql, params = await task
            with self.db.transaction() as conn:
                conn.execute(sql, params)
                conn.execute(
                    "UPDATE jobs SET status = 'succeeded', progress = ?, finished_at = ?"
                    " WHERE id = ?",
                    (latest[0] if latest else None, utc_now(), job_id),
                )
        except asyncio.CancelledError:
            if job_id in self._cancel_requested:
                with self.db.transaction() as conn:
                    self._finish(conn, job, "cancelled")
            else:  # the worker itself is being stopped
                with self.db.transaction() as conn:
                    self._finish(conn, job, "failed", INTERRUPTED)
                raise
        except (RunnerError, ModError, PushError, SwitchError) as exc:
            with self.db.transaction() as conn:
                self._finish(conn, job, "failed", str(exc))
        except Exception:
            log.exception("job %s failed unexpectedly", job_id)
            with self.db.transaction() as conn:
                self._finish(conn, job, "failed", "internal error")
        finally:
            self._current = None
            self._cancel_requested.discard(job_id)
            shutil.rmtree(workdir, ignore_errors=True)
            await self._bump()

    def _seed_dir(self, job: dict[str, Any]) -> Path:
        return self.seeds_dir / job["owner_id"] / job["seed_id"]

    # Each kind of work returns the update that records its result, which is applied
    # in the same transaction as marking the job succeeded.

    async def _generate(self, job: dict[str, Any], workdir: Path, on_progress: Any) -> _Update:
        request = self._request_for(job, workdir)
        result = await self.runner.generate(request, workdir, on_progress)
        final = self._seed_dir(job) / "seed.rdvgame"
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(result.rdvgame, final)
        return (
            "UPDATE seeds SET status = 'ready', permalink = ?, seed_hash = ?,"
            " completed_at = ? WHERE id = ?",
            (result.permalink, result.seed_hash, utc_now(), job["seed_id"]),
        )

    async def _export(self, job: dict[str, Any], workdir: Path, on_progress: Any) -> _Update:
        romfs = self.romfs
        if self.romfs_busy():
            raise RunnerError("new game files are being installed; patch again when that finishes")
        if romfs is None:
            raise RunnerError("patching isn't available on this server")
        # Checked again here: the game files may have changed since the job was queued.
        game_files = await asyncio.to_thread(romfs.status)
        if not game_files.ready:
            raise RunnerError(f"the game files aren't ready: {game_files.message}")
        rdvgame = self._seed_dir(job) / "seed.rdvgame"
        if not rdvgame.is_file():
            raise RunnerError("the seed file is missing")
        result = await self.runner.export(
            ExportRequest(rdvgame=rdvgame, romfs=romfs.root), workdir, on_progress
        )
        on_progress("Checking the patched files")
        info = await asyncio.to_thread(finalize_mod, result.mod, self._seed_dir(job) / "mod")
        return (
            "UPDATE mods SET status = 'ready', game_version = ?, file_count = ?,"
            " size_bytes = ?, completed_at = ? WHERE seed_id = ?",
            (game_files.version, info.file_count, info.size_bytes, utc_now(), job["seed_id"]),
        )

    async def _push(self, job: dict[str, Any], workdir: Path, on_progress: Any) -> _Update:
        """Send a patched seed to a Switch over FTP, in a worker thread (ADR 0016)."""
        params = json.loads(job["params"] or "{}")
        switch_id = params.get("switch_id", "")
        owner = job["owner_id"]
        mod_dir = self._seed_dir(job) / "mod"
        if not mod_dir.is_dir():
            raise PushError("the patched files are missing: patch the seed again")
        with self.db.connect() as conn:
            target = row_to_dict(
                conn.execute(
                    "SELECT host, port, username, password FROM switches"
                    " WHERE id = ? AND owner_id = ?",
                    (switch_id, owner),
                ).fetchone()
            )
            installed = conn.execute(
                "SELECT files FROM switch_installs WHERE switch_id = ?", (switch_id,)
            ).fetchone()
        if target is None:
            raise PushError("the Switch was removed before the seed could be sent")
        previous = json.loads(installed["files"]) if installed else []
        options = PushOptions(
            overwrite=bool(params.get("overwrite")), clear=bool(params.get("clear"))
        )

        loop = asyncio.get_running_loop()
        stop = threading.Event()

        def record_planned(files: list[str]) -> None:
            self._record_install(owner, switch_id, job["seed_id"], files, "installing")

        def progress(p: Progress) -> None:
            loop.call_soon_threadsafe(on_progress, p.describe())

        def send() -> list[str]:
            loop.call_soon_threadsafe(on_progress, "Connecting to the Switch")
            ftp = switches.connect(target, self.switch_resolver, ftplib.FTP)
            try:
                return push(
                    ftp, mod_dir, seed_id=job["seed_id"], previous=previous, options=options,
                    record_planned=record_planned, on_progress=progress, cancelled=stop.is_set,
                )  # fmt: skip
            except (ftplib.Error, OSError, EOFError) as exc:
                raise PushError(f"the connection to the Switch was lost ({exc})") from None
            finally:
                with contextlib.suppress(ftplib.Error, OSError, EOFError):
                    ftp.quit()

        sending = asyncio.ensure_future(asyncio.to_thread(send))
        try:
            files = await asyncio.shield(sending)
        except asyncio.CancelledError:
            stop.set()  # the thread stops before its next file
            with contextlib.suppress(Exception):
                await sending
            raise
        on_progress("Sent. Close ftpd and launch Dread with a new save file.")
        return (
            "INSERT INTO switch_installs (switch_id, owner_id, seed_id, files, state, updated_at)"
            " VALUES (?, ?, ?, ?, 'installed', ?) ON CONFLICT (switch_id) DO UPDATE SET"
            " seed_id = excluded.seed_id, files = excluded.files, state = 'installed',"
            " updated_at = excluded.updated_at",
            (switch_id, owner, job["seed_id"], json.dumps(files), utc_now()),
        )

    def _record_install(
        self, owner: str, switch_id: str, seed_id: str, files: list[str], state: str
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO switch_installs"
                " (switch_id, owner_id, seed_id, files, state, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (switch_id) DO UPDATE SET"
                " seed_id = excluded.seed_id, files = excluded.files, state = excluded.state,"
                " updated_at = excluded.updated_at",
                (switch_id, owner, seed_id, json.dumps(files), state, utc_now()),
            )

    def _request_for(self, job: dict[str, Any], workdir: Path) -> GenerateRequest:
        key = job["preset_key"]
        if key is None:
            return GenerateRequest(permalink=job["requested_permalink"])
        if is_user_preset(key):
            content = self.presets.content(job["owner_id"], key)
            if content is None:
                raise RunnerError("the preset was deleted before this seed could be generated")
            preset_file = workdir / "preset.rdvpreset"
            preset_file.write_text(content, encoding="utf-8")
        else:
            preset_file = bundled_dread_presets()[key].path
        return GenerateRequest(
            preset_file=preset_file, seed_number=job["seed_number"], race=bool(job["race"])
        )

    def _set_progress(self, job_id: str, line: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (line, job_id))

    @staticmethod
    def _finish(conn: Any, job: dict[str, Any], status: str, error: str | None = None) -> None:
        """Mark a job, and the seed or mod it was making, failed or cancelled."""
        now = utc_now()
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, now, job["id"]),
        )
        if job["kind"] == "push":
            return  # a failed send changes nothing about the seed or its mod
        table = "seeds" if job["kind"] == "generate" else "mods"
        key = "id" if table == "seeds" else "seed_id"
        conn.execute(
            f"UPDATE {table} SET status = ?, completed_at = ? WHERE {key} = ?",  # noqa: S608
            (status, now, job["seed_id"]),
        )
