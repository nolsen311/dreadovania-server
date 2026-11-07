# SPDX-License-Identifier: GPL-3.0-or-later
"""The Randovania subprocess runner (ADR 0011 §2)."""

import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import make_user, wait_until

from dreadovania.jobs import JobQueue
from dreadovania.presets import bundled_dread_presets
from dreadovania.runner import GenerateRequest, RandovaniaRunner, RunnerError

pytestmark = pytest.mark.anyio

# A stand-in for "python -m randovania": behaviour is chosen by the first argument.
FAKE = textwrap.dedent(
    """
    import json, os, subprocess, sys, time
    mode, output = sys.argv[1], sys.argv[-1]
    if mode == "ok":
        print("Permalink: FAKE-perma_link", flush=True)
        print("Creating item pool", flush=True)
        json.dump({"schema_version": 1, "info": {}}, open(output, "w"))
        print("Took 0.1 seconds. Hash: ABCD1234", flush=True)
    elif mode == "crash":
        print("Permalink: X", flush=True)
        print("Traceback: something broke", file=sys.stderr, flush=True)
        sys.exit(3)
    elif mode == "no-hash":
        json.dump({"schema_version": 1, "info": {}}, open(output, "w"))
    elif mode == "bad-output":
        print("Permalink: X\\nHash: Y", flush=True)
        open(output, "w").write("not json")
    elif mode == "hang":
        # Start a grandchild too, to prove the whole process group is killed.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open(output + ".pids", "w").write(f"{os.getpid()} {child.pid}")
        print("Permalink: X", flush=True)
        time.sleep(60)
    """
)


@pytest.fixture
def fake_cli(tmp_path: Path):
    script = tmp_path / "fake_randovania.py"
    script.write_text(FAKE)
    return lambda mode, **kw: RandovaniaRunner(command=[sys.executable, str(script), mode], **kw)


REQUEST = GenerateRequest(preset_file=Path("p.rdvpreset"), seed_number=7)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); check its state.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


def test_arguments_for_a_preset() -> None:
    args = RandovaniaRunner(command=["rdv"]).build_args(
        GenerateRequest(preset_file=Path("/p/x.rdvpreset"), seed_number=5, race=True), Path("o")
    )
    assert args == [
        "rdv", "layout", "generate-from-presets", "--status-update",
        "--file", "/p/x.rdvpreset", "--seed-number", "5", "--race", "o",
    ]  # fmt: skip


def test_permalink_is_a_single_argument_so_it_cannot_be_a_flag() -> None:
    args = RandovaniaRunner(command=["rdv"]).build_args(
        GenerateRequest(permalink="--race"), Path("o")
    )
    assert "--permalink=--race" in args
    assert "--race" not in args


async def test_success_reports_permalink_hash_and_progress(fake_cli, tmp_path) -> None:
    lines: list[str] = []
    result = await fake_cli("ok").generate(REQUEST, tmp_path, lines.append)
    assert (result.permalink, result.seed_hash) == ("FAKE-perma_link", "ABCD1234")
    assert result.rdvgame == tmp_path / "seed.rdvgame"
    assert "Creating item pool" in lines


async def test_nonzero_exit_reports_the_last_error_line(fake_cli, tmp_path) -> None:
    with pytest.raises(RunnerError, match=r"exit code 3\): Traceback: something broke"):
        await fake_cli("crash").generate(REQUEST, tmp_path, lambda _: None)


async def test_missing_hash_is_an_error(fake_cli, tmp_path) -> None:
    with pytest.raises(RunnerError, match="without reporting a permalink and hash"):
        await fake_cli("no-hash").generate(REQUEST, tmp_path, lambda _: None)


async def test_invalid_output_is_an_error(fake_cli, tmp_path) -> None:
    with pytest.raises(RunnerError, match="not valid JSON"):
        await fake_cli("bad-output").generate(REQUEST, tmp_path, lambda _: None)


async def test_timeout_kills_the_whole_process_group(fake_cli, tmp_path) -> None:
    with pytest.raises(RunnerError, match="timed out"):
        await fake_cli("hang", timeout=1.5).generate(REQUEST, tmp_path, lambda _: None)
    parent, child = map(int, (tmp_path / "seed.rdvgame.pids").read_text().split())
    await wait_until(lambda: not alive(parent) and not alive(child), timeout=5)


async def test_cancellation_kills_the_whole_process_group(fake_cli, tmp_path) -> None:
    pids = tmp_path / "seed.rdvgame.pids"
    task = asyncio.create_task(fake_cli("hang").generate(REQUEST, tmp_path, lambda _: None))
    await wait_until(pids.exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    parent, child = map(int, pids.read_text().split())
    await wait_until(lambda: not alive(parent) and not alive(child), timeout=5)


async def test_randovania_runs_at_lower_priority(fake_cli, tmp_path) -> None:
    runner = fake_cli("hang", niceness=7)
    pids = tmp_path / "seed.rdvgame.pids"
    task = asyncio.create_task(runner.generate(REQUEST, tmp_path, lambda _: None))
    await wait_until(pids.exists)
    parent = int(pids.read_text().split()[0])
    assert os.getpriority(os.PRIO_PROCESS, parent) >= os.getpriority(os.PRIO_PROCESS, 0) + 7
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---- real Randovania (slow: tens of seconds each) ------------------------------


async def test_real_randovania_through_the_job_queue(db, tmp_path) -> None:
    """End to end: queue → real Randovania → stored seed (ADR 0011 §6)."""
    queue = JobQueue(db, RandovaniaRunner(), db.path.parent, "12.0.0", progress_interval=0.2)
    owner = make_user(db, "alice")
    worker = asyncio.create_task(queue.run_worker())
    try:
        sub = queue.submit_generate(owner, preset_key="starter_preset", seed_number=12345)
        await wait_until(
            lambda: queue.get_job(sub.job_id, owner)["status"] in ("succeeded", "failed"), 300
        )
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    job = queue.get_job(sub.job_id, owner)
    assert job["status"] == "succeeded", job["error"]
    assert job["progress"]  # live progress was recorded
    with db.connect() as conn:
        s = conn.execute("SELECT * FROM seeds WHERE id = ?", (sub.seed_id,)).fetchone()
    assert s["status"] == "ready"
    assert s["permalink"] and s["seed_hash"]
    assert (queue.seeds_dir / owner / sub.seed_id / "seed.rdvgame").stat().st_size > 1000


async def test_real_randovania_treats_a_flag_like_permalink_as_data(tmp_path) -> None:
    """'--race' as a permalink is rejected by Randovania as a bad permalink, not obeyed."""
    with pytest.raises(RunnerError, match="exit code"):
        await RandovaniaRunner().generate(
            GenerateRequest(permalink="--race"), tmp_path, lambda _: None
        )
    assert not (tmp_path / "seed.rdvgame").exists()


def test_bundled_dread_presets_include_the_starter_preset() -> None:
    presets = bundled_dread_presets()
    assert "starter_preset" in presets
    assert presets["starter_preset"].path.is_file()
    assert presets["starter_preset"].name
