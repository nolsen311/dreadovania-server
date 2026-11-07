# SPDX-License-Identifier: GPL-3.0-or-later
"""The job queue, with a fake runner: fast and deterministic (ADR 0011 §6)."""

import asyncio
import threading

import pytest
from conftest import FakeRunner, make_user, wait_until

from dreadovania.db import Database
from dreadovania.jobs import INTERRUPTED, JobQueue, QueueFull
from dreadovania.validation import MAX_SEED_NUMBER, InvalidInput

pytestmark = pytest.mark.anyio


def seed(db: Database, seed_id: str) -> dict:
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM seeds WHERE id = ?", (seed_id,)).fetchone())


async def test_generate_from_preset_end_to_end(db, queue, fake_runner, worker) -> None:
    owner = make_user(db, "alice")
    sub = queue.submit_generate(owner, preset_key="starter_preset", seed_number=42)

    await wait_until(lambda: queue.get_job(sub.job_id, owner)["status"] == "succeeded")
    job = queue.get_job(sub.job_id, owner)
    assert job["progress"] == "Resolving... 1 total resources"
    assert job["started_at"] and job["finished_at"] and job["error"] is None

    s = seed(db, sub.seed_id)
    assert (s["status"], s["permalink"], s["seed_hash"]) == ("ready", "FAKEPERMALINK", "HASH1234")
    assert (s["seed_number"], s["race"], s["randovania_version"]) == (42, 0, "12.0.0")
    stored = queue.seeds_dir / owner / sub.seed_id / "seed.rdvgame"
    assert stored.is_file()

    request = fake_runner.requests[0]
    assert request.preset_file.name == "starter_preset.rdvpreset"
    assert (request.seed_number, request.race) == (42, False)
    assert not any(queue.work_dir.iterdir())  # the work directory was cleaned up


async def test_random_seed_number_is_recorded(db, queue, worker) -> None:
    owner = make_user(db, "alice")
    sub = queue.submit_generate(owner, preset_key="starter_preset")
    await wait_until(lambda: seed(db, sub.seed_id)["status"] == "ready")
    assert 0 <= seed(db, sub.seed_id)["seed_number"] < MAX_SEED_NUMBER


async def test_generate_from_permalink(db, queue, fake_runner, worker) -> None:
    owner = make_user(db, "alice")
    sub = queue.submit_generate(owner, permalink="  DWT2Y0yRC4rILFDaWMyRyTb8AAHo  ")
    await wait_until(lambda: seed(db, sub.seed_id)["status"] == "ready")
    assert fake_runner.requests[0].permalink == "DWT2Y0yRC4rILFDaWMyRyTb8AAHo"
    assert seed(db, sub.seed_id)["requested_permalink"] == "DWT2Y0yRC4rILFDaWMyRyTb8AAHo"


async def test_race_flag_reaches_the_runner(db, queue, fake_runner, worker) -> None:
    owner = make_user(db, "alice")
    sub = queue.submit_generate(owner, preset_key="starter_preset", seed_number=1, race=True)
    await wait_until(lambda: seed(db, sub.seed_id)["status"] == "ready")
    assert fake_runner.requests[0].race is True
    assert seed(db, sub.seed_id)["race"] == 1


async def test_runner_failure_marks_job_and_seed_failed(db, queue, fake_runner, worker) -> None:
    owner = make_user(db, "alice")
    fake_runner.fail_with = "Randovania failed (exit code 1): boom"
    sub = queue.submit_generate(owner, preset_key="starter_preset", seed_number=1)
    await wait_until(lambda: queue.get_job(sub.job_id, owner)["status"] == "failed")
    assert queue.get_job(sub.job_id, owner)["error"] == "Randovania failed (exit code 1): boom"
    assert seed(db, sub.seed_id)["status"] == "failed"


async def test_jobs_run_one_at_a_time_in_order(db, queue, fake_runner, worker) -> None:
    owner = make_user(db, "alice")
    fake_runner.release.clear()
    first = queue.submit_generate(owner, preset_key="starter_preset", seed_number=1)
    second = queue.submit_generate(owner, preset_key="starter_preset", seed_number=2)
    await fake_runner.started.wait()
    assert queue.get_job(first.job_id, owner)["status"] == "running"
    assert queue.get_job(second.job_id, owner)["status"] == "queued"
    fake_runner.release.set()
    await wait_until(lambda: queue.get_job(second.job_id, owner)["status"] == "succeeded")
    assert [r.seed_number for r in fake_runner.requests] == [1, 2]


async def test_queue_limit_per_owner(db: Database, fake_runner: FakeRunner) -> None:
    queue = JobQueue(db, fake_runner, db.path.parent, "12.0.0", max_queued_per_owner=2)
    alice, bob = make_user(db, "alice"), make_user(db, "bob")
    queue.submit_generate(alice, preset_key="starter_preset")
    queue.submit_generate(alice, preset_key="starter_preset")
    with pytest.raises(QueueFull):
        queue.submit_generate(alice, preset_key="starter_preset")
    queue.submit_generate(bob, preset_key="starter_preset")  # limits are per owner


async def test_jobs_are_owner_scoped(db, queue) -> None:
    alice, bob = make_user(db, "alice"), make_user(db, "bob")
    sub = queue.submit_generate(alice, preset_key="starter_preset")
    assert queue.get_job(sub.job_id, alice) is not None
    assert queue.get_job(sub.job_id, bob) is None
    assert queue.cancel(sub.job_id, bob) is False
    assert queue.get_job(sub.job_id, alice)["status"] == "queued"


async def test_cancel_queued_job(db, queue) -> None:
    owner = make_user(db, "alice")
    sub = queue.submit_generate(owner, preset_key="starter_preset")
    assert queue.cancel(sub.job_id, owner) is True
    assert queue.get_job(sub.job_id, owner)["status"] == "cancelled"
    assert seed(db, sub.seed_id)["status"] == "cancelled"
    assert queue.cancel(sub.job_id, owner) is False  # nothing left to cancel


async def test_cancel_running_job(db, queue, fake_runner, worker) -> None:
    owner = make_user(db, "alice")
    fake_runner.release.clear()  # the fake generation blocks until cancelled
    sub = queue.submit_generate(owner, preset_key="starter_preset")
    await fake_runner.started.wait()
    assert queue.cancel(sub.job_id, owner) is True
    await wait_until(lambda: queue.get_job(sub.job_id, owner)["status"] == "cancelled")
    assert seed(db, sub.seed_id)["status"] == "cancelled"
    # The worker keeps going after a cancellation.
    fake_runner.release.set()
    nxt = queue.submit_generate(owner, preset_key="starter_preset")
    await wait_until(lambda: queue.get_job(nxt.job_id, owner)["status"] == "succeeded")


async def test_submit_from_another_thread_wakes_the_worker(db, queue, worker) -> None:
    owner = make_user(db, "alice")
    await asyncio.sleep(0.05)  # let the worker go idle
    result = {}
    thread = threading.Thread(
        target=lambda: result.update(sub=queue.submit_generate(owner, preset_key="starter_preset"))
    )
    thread.start()
    thread.join()
    await wait_until(lambda: queue.get_job(result["sub"].job_id, owner)["status"] == "succeeded")


async def test_wait_for_change_reports_progress(db, queue, fake_runner, worker) -> None:
    owner = make_user(db, "alice")
    before = queue.version
    queue.submit_generate(owner, preset_key="starter_preset")
    assert await queue.wait_for_change(before, timeout=5) != before


async def test_stopping_the_worker_marks_the_running_job_interrupted(db, queue, fake_runner):
    owner = make_user(db, "alice")
    fake_runner.release.clear()
    task = asyncio.create_task(queue.run_worker())
    sub = queue.submit_generate(owner, preset_key="starter_preset")
    await fake_runner.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = queue.get_job(sub.job_id, owner)
    assert (job["status"], job["error"]) == ("failed", INTERRUPTED)


def test_recover_interrupted_marks_running_jobs_failed(db, queue) -> None:
    owner = make_user(db, "alice")
    sub = queue.submit_generate(owner, preset_key="starter_preset")
    with db.connect() as conn:  # simulate a crash while the job was running
        conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (sub.job_id,))
    (queue.work_dir / sub.job_id).mkdir(parents=True)

    assert queue.recover_interrupted() == 1
    job = queue.get_job(sub.job_id, owner)
    assert (job["status"], job["error"]) == ("failed", INTERRUPTED)
    assert seed(db, sub.seed_id)["status"] == "failed"
    assert not queue.work_dir.exists()  # leftovers from the crash are removed


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "either a preset or a permalink"),
        ({"preset_key": "starter_preset", "permalink": "ABCD"}, "either a preset or a permalink"),
        ({"preset_key": "../../etc/passwd"}, "unknown preset"),
        ({"preset_key": "starter_preset", "seed_number": -1}, "between 0 and"),
        ({"preset_key": "starter_preset", "seed_number": MAX_SEED_NUMBER}, "between 0 and"),
        ({"preset_key": "starter_preset", "seed_number": True}, "whole number"),
        ({"permalink": "abc; rm -rf /"}, "permalink"),
        ({"permalink": "A" * 5000}, "permalink"),
        ({"permalink": "ABCDEFGH", "seed_number": 3}, "already contains"),
    ],
)
def test_invalid_input_is_rejected_before_anything_is_stored(db, queue, kwargs, message) -> None:
    owner = make_user(db, "alice")
    with pytest.raises(InvalidInput, match=message):
        queue.submit_generate(owner, **kwargs)
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM seeds").fetchone()[0] == 0
