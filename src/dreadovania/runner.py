# SPDX-License-Identifier: GPL-3.0-or-later
"""Runs the Randovania CLI as a subprocess (ADR 0010, ADR 0011 §2).

Arguments are always passed as a list, never through a shell. Inputs are
validated before they get here (see ``validation``), the process gets a lower
CPU priority, a timeout, and its own process group so cancellation kills
everything it started.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

DEFAULT_COMMAND: tuple[str, ...] = (sys.executable, "-m", "randovania")
# For what the CLI can't do: patcher data with cosmetic options (ADR 0002, ADR 0025).
DEFAULT_ADAPTER: tuple[str, ...] = (sys.executable, "-m", "dreadovania.randovania_adapter")
MAX_RDVGAME_BYTES = 50 * 1024 * 1024
_PERMALINK_LINE = re.compile(r"^Permalink: (\S+)$")
_HASH_LINE = re.compile(r"Hash: (\S+)$")


class RunnerError(Exception):
    """Generation or export failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class GenerateRequest:
    preset_file: Path | None = None
    permalink: str | None = None
    seed_number: int | None = None
    race: bool = False

    def __post_init__(self) -> None:
        if (self.preset_file is None) == (self.permalink is None):
            raise ValueError("exactly one of preset_file or permalink is required")
        if self.preset_file is not None and self.seed_number is None:
            raise ValueError("a seed number is required when generating from a preset")


@dataclass(frozen=True)
class GenerateResult:
    permalink: str
    seed_hash: str
    rdvgame: Path


@dataclass(frozen=True)
class ExportRequest:
    """Patch a generated seed into the user's RomFS, for Atmosphere (ADR 0013 §2)."""

    rdvgame: Path
    romfs: Path
    # Randovania's cosmetic options as JSON (ADR 0025); None for its defaults.
    cosmetics: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExportResult:
    # Laid out like the SD card's atmosphere/ folder.
    mod: Path


ProgressCallback = Callable[[str], None]


class Runner(Protocol):
    async def generate(
        self, request: GenerateRequest, workdir: Path, on_progress: ProgressCallback
    ) -> GenerateResult: ...

    async def export(
        self, request: ExportRequest, workdir: Path, on_progress: ProgressCallback
    ) -> ExportResult: ...


class RandovaniaRunner:
    def __init__(
        self,
        command: Sequence[str] = DEFAULT_COMMAND,
        timeout: float = 600,
        niceness: int = 10,
        export_timeout: float = 1800,
        adapter_command: Sequence[str] = DEFAULT_ADAPTER,
    ):
        self.command = tuple(command)
        self.adapter_command = tuple(adapter_command)
        self.timeout = timeout
        self.export_timeout = export_timeout
        self.niceness = niceness

    def build_args(self, request: GenerateRequest, output: Path) -> list[str]:
        if request.permalink is not None:
            # "--permalink=VALUE" as one argument: a permalink may legitimately start
            # with "-" (it's in the base64 alphabet), and must never parse as a flag.
            return [
                *self.command, "layout", "generate-from-permalink", "--status-update",
                f"--permalink={request.permalink}", str(output),
            ]  # fmt: skip
        args = [
            *self.command, "layout", "generate-from-presets", "--status-update",
            "--file", str(request.preset_file), "--seed-number", str(request.seed_number),
        ]  # fmt: skip
        if request.race:
            args.append("--race")
        return [*args, str(output)]

    def build_export_args(
        self, request: ExportRequest, patcher_data: Path, output: Path
    ) -> tuple[list[str], list[str]]:
        """The two commands: write the patcher data, then patch the game with it.

        ``--use-exlaunch`` matches what Randovania's own export dialog always uses.
        With cosmetic options, the patcher data comes from the adapter, which reads
        them from ``cosmetics.json`` next to the patcher data.
        """
        if request.cosmetics is None:
            data = [
                *self.command, "layout", "patcher-data",
                "--layout-file", str(request.rdvgame), "--output", str(patcher_data),
            ]  # fmt: skip
        else:  # the CLI's patcher-data only knows the default cosmetics
            data = [
                *self.adapter_command, "patcher-data", "--layout-file", str(request.rdvgame),
                "--cosmetics", str(patcher_data.with_name("cosmetics.json")),
                "--output", str(patcher_data),
            ]  # fmt: skip
        export = [
            *self.command, "layout", "export-game", "--patcher-data", str(patcher_data), "dread",
            "--input-path", str(request.romfs), "--output-path", str(output),
            "--target-platform", "atmosphere", "--use-exlaunch",
        ]  # fmt: skip
        return data, export

    async def generate(
        self, request: GenerateRequest, workdir: Path, on_progress: ProgressCallback
    ) -> GenerateResult:
        output = workdir / "seed.rdvgame"
        permalink: str | None = None
        seed_hash: str | None = None

        def on_line(line: str) -> None:
            nonlocal permalink, seed_hash
            if match := _PERMALINK_LINE.match(line):
                permalink = match.group(1)
            elif match := _HASH_LINE.search(line):
                seed_hash = match.group(1)
            on_progress(line[:300])

        await self._run(
            self.build_args(request, output), workdir, on_line, self.timeout, "generation"
        )
        if permalink is None or seed_hash is None:
            raise RunnerError("Randovania finished without reporting a permalink and hash")
        self._validate_output(output)
        return GenerateResult(permalink=permalink, seed_hash=seed_hash, rdvgame=output)

    async def export(
        self, request: ExportRequest, workdir: Path, on_progress: ProgressCallback
    ) -> ExportResult:
        patcher_data = workdir / "patcher-data.json"
        output = workdir / "mod"
        data_args, export_args = self.build_export_args(request, patcher_data, output)
        if request.cosmetics is not None:
            patcher_data.with_name("cosmetics.json").write_text(json.dumps(request.cosmetics))
        try:
            async with asyncio.timeout(self.export_timeout):
                on_progress("Preparing the patch")
                # Patcher data goes to a file, never to our logs: it describes the layout.
                await self._run(data_args, workdir, lambda _line: None, None, "preparing")
                if not patcher_data.is_file():
                    raise RunnerError("Randovania didn't write the patcher data")
                await self._run(
                    export_args, workdir, lambda line: on_progress(line[:300]), None, "patching"
                )
        except TimeoutError:
            raise RunnerError(
                f"patching timed out after {self.export_timeout:.0f} seconds"
            ) from None
        if not output.is_dir():
            raise RunnerError("Randovania finished without writing the patched files")
        return ExportResult(mod=output)

    async def _run(
        self,
        args: list[str],
        workdir: Path,
        on_line: Callable[[str], None],
        timeout: float | None,
        what: str,
    ) -> None:
        """Run one Randovania command; raise RunnerError unless it succeeds."""
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=workdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group, so we can kill all of it
        )
        self._lower_priority(process.pid)
        stderr_tail: deque[str] = deque(maxlen=20)

        async def read_stdout() -> None:
            if process.stdout is None:
                return
            async for raw in process.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    on_line(line)

        async def read_stderr() -> None:
            if process.stderr is None:
                return
            async for raw in process.stderr:
                stderr_tail.append(raw.decode("utf-8", "replace").rstrip()[:300])

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(read_stdout(), read_stderr())
                returncode = await process.wait()
        except TimeoutError:
            self._kill(process)
            await process.wait()
            raise RunnerError(f"{what} timed out after {timeout:.0f} seconds") from None
        except BaseException:
            # Cancelled, an outer timeout, or anything unexpected: never leave it running.
            self._kill(process)
            with contextlib.suppress(Exception):
                await asyncio.shield(process.wait())
            raise

        if returncode != 0:
            log.warning("randovania exited with %s: %s", returncode, " | ".join(stderr_tail))
            last = stderr_tail[-1] if stderr_tail else "no error output"
            raise RunnerError(f"Randovania failed (exit code {returncode}): {last}")

    def _lower_priority(self, pid: int) -> None:
        if self.niceness <= 0:
            return
        try:
            os.setpriority(os.PRIO_PROCESS, pid, self.niceness)
        except OSError as exc:  # not fatal: generation still works, just less politely
            log.warning("could not lower the priority of Randovania (pid %s): %s", pid, exc)

    @staticmethod
    def _kill(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    @staticmethod
    def _validate_output(output: Path) -> None:
        if not output.is_file():
            raise RunnerError("Randovania finished without writing a seed file")
        size = output.stat().st_size
        if not 0 < size <= MAX_RDVGAME_BYTES:
            raise RunnerError(f"the seed file has an unexpected size ({size} bytes)")
        try:
            data = json.loads(output.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerError("the seed file is not valid JSON") from exc
        if not isinstance(data, dict) or "schema_version" not in data or "info" not in data:
            raise RunnerError("the seed file is not a Randovania game file")
