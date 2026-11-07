# SPDX-License-Identifier: GPL-3.0-or-later
"""The persistent, single-worker job queue (ADR 0011 §2).

The queue is the ``jobs`` table, so queued work survives a restart. One worker
runs one Randovania process at a time. A job found ``running`` at startup was
interrupted; it is marked failed and never silently re-run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dreadovania import validation
from dreadovania.db import Database, row_to_dict, utc_now
from dreadovania.presets import bundled_dread_presets
from dreadovania.runner import GenerateRequest, Runner, RunnerError

log = logging.getLogger(__name__)

INTERRUPTED = "interrupted: the server stopped while this job was running"


class QueueFull(Exception):
    pass


@dataclass(frozen=True)
class Submitted:
    seed_id: str
    job_id: str


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
    ):
        self.db = db
        self.runner = runner
        self.seeds_dir = data_dir / "seeds"
        self.work_dir = data_dir / "work"
        self.randovania_version = randovania_version
        self.max_queued_per_owner = max_queued_per_owner
        self.progress_interval = progress_interval

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
            if preset_key not in bundled_dread_presets():
                raise validation.InvalidInput("unknown preset")
            seed_number = validation.check_seed_number(
                seed_number if seed_number is not None
                else secrets.randbelow(validation.MAX_SEED_NUMBER)
            )  # fmt: skip

        seed_id, job_id, now = new_id(), new_id(), utc_now()
        with self.db.transaction() as conn:
            active = conn.execute(
                "SELECT count(*) FROM jobs WHERE owner_id = ? AND status IN ('queued', 'running')",
                (owner_id,),
            ).fetchone()[0]
            if active >= self.max_queued_per_owner:
                raise QueueFull(
                    f"you already have {active} seeds waiting; try again when one finishes"
                )
            conn.execute(
                "INSERT INTO seeds (id, owner_id, status, source, preset_key, requested_permalink,"
                " seed_number, race, randovania_version, created_at)"
                " VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (seed_id, owner_id, "preset" if preset_key else "permalink", preset_key,
                 permalink, seed_number, int(race), self.randovania_version, now),
            )  # fmt: skip
            conn.execute(
                "INSERT INTO jobs (id, owner_id, seed_id, kind, status, created_at)"
                " VALUES (?, ?, ?, 'generate', 'queued', ?)",
                (job_id, owner_id, seed_id, now),
            )
        self._notify_from_any_thread()
        return Submitted(seed_id=seed_id, job_id=job_id)

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
                "SELECT status, seed_id FROM jobs WHERE id = ? AND owner_id = ?",
                (job_id, owner_id),
            ).fetchone()
            if row is None or row["status"] not in ("queued", "running"):
                return False
            if row["status"] == "queued":
                self._finish(conn, job_id, row["seed_id"], "cancelled", "cancelled")
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
            rows = conn.execute("SELECT id, seed_id FROM jobs WHERE status = 'running'").fetchall()
            for row in rows:
                self._finish(conn, row["id"], row["seed_id"], "failed", "failed", INTERRUPTED)
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
                "SELECT j.id, j.owner_id, j.seed_id, s.preset_key, s.requested_permalink,"
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
        job_id, seed_id = job["id"], job["seed_id"]
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

        if job["preset_key"] is not None:
            request = GenerateRequest(
                preset_file=bundled_dread_presets()[job["preset_key"]].path,
                seed_number=job["seed_number"],
                race=bool(job["race"]),
            )
        else:
            request = GenerateRequest(permalink=job["requested_permalink"])

        task = asyncio.create_task(self.runner.generate(request, workdir, on_progress))
        self._current = (job_id, task)
        if job_id in self._cancel_requested:
            task.cancel()  # cancelled between being claimed and starting
        try:
            result = await task
            final = self.seeds_dir / job["owner_id"] / seed_id / "seed.rdvgame"
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(result.rdvgame, final)
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE seeds SET status = 'ready', permalink = ?, seed_hash = ?,"
                    " completed_at = ? WHERE id = ?",
                    (result.permalink, result.seed_hash, utc_now(), seed_id),
                )
                conn.execute(
                    "UPDATE jobs SET status = 'succeeded', progress = ?, finished_at = ?"
                    " WHERE id = ?",
                    (latest[0] if latest else None, utc_now(), job_id),
                )
        except asyncio.CancelledError:
            if job_id in self._cancel_requested:
                with self.db.transaction() as conn:
                    self._finish(conn, job_id, seed_id, "cancelled", "cancelled")
            else:  # the worker itself is being stopped
                with self.db.transaction() as conn:
                    self._finish(conn, job_id, seed_id, "failed", "failed", INTERRUPTED)
                raise
        except RunnerError as exc:
            with self.db.transaction() as conn:
                self._finish(conn, job_id, seed_id, "failed", "failed", str(exc))
        except Exception:
            log.exception("job %s failed unexpectedly", job_id)
            with self.db.transaction() as conn:
                self._finish(conn, job_id, seed_id, "failed", "failed", "internal error")
        finally:
            self._current = None
            self._cancel_requested.discard(job_id)
            shutil.rmtree(workdir, ignore_errors=True)
            await self._bump()

    def _set_progress(self, job_id: str, line: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (line, job_id))

    @staticmethod
    def _finish(conn: Any, job_id: str, seed_id: str, job_status: str, seed_status: str,
                error: str | None = None) -> None:  # fmt: skip
        now = utc_now()
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (job_status, error, now, job_id),
        )
        conn.execute(
            "UPDATE seeds SET status = ?, completed_at = ? WHERE id = ?",
            (seed_status, now, seed_id),
        )
