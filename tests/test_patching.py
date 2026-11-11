# SPDX-License-Identifier: GPL-3.0-or-later
"""Patching seeds into the user's game files (ADR 0013, Phase 2 PR 2).

No real game data is used: the RomFS is synthetic, version detection is
injected, and the fake runner writes a mod with the patcher's layout.
"""

import asyncio
import contextlib
import io
import json
import shutil
import sqlite3
import sys
import textwrap
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from conftest import (
    FAKE_MOD_BYTES,
    FAKE_MOD_FILES,
    FakeRunner,
    make_user,
    wait_until,
    write_fake_mod,
)
from fastapi.testclient import TestClient
from test_api import ORIGIN, H, wait_for_job
from test_romfs import fake_identify, make_romfs

from dreadovania import auth, mods
from dreadovania.app import create_app
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.jobs import INTERRUPTED, CannotPatch, JobQueue
from dreadovania.romfs import RomfsInspector
from dreadovania.runner import ExportRequest, RandovaniaRunner, RunnerError
from dreadovania.seeds import SeedBusy, SeedStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def romfs_dir(tmp_path: Path) -> Path:
    return make_romfs(tmp_path / "romfs")


@pytest.fixture
def pqueue(db: Database, fake_runner: FakeRunner, romfs_dir: Path) -> JobQueue:
    return JobQueue(
        db, fake_runner, db.path.parent, randovania_version="12.0.0", progress_interval=0,
        romfs=RomfsInspector(romfs_dir, identify=fake_identify),
    )  # fmt: skip


@pytest.fixture
async def pworker(pqueue: JobQueue) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(pqueue.run_worker())
    yield task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def ready_seed(queue: JobQueue, owner: str, race: bool = False) -> str:
    sub = queue.submit_generate(owner, preset_key="starter_preset", seed_number=1, race=race)
    await wait_until(lambda: queue.get_job(sub.job_id, owner)["status"] == "succeeded")
    return sub.seed_id


def mod_row(db: Database, seed_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM mods WHERE seed_id = ?", (seed_id,)).fetchone()
    return dict(row) if row else None


# ---- the queue -------------------------------------------------------------------------------


async def test_patching_end_to_end(db, pqueue, fake_runner, pworker, romfs_dir) -> None:
    owner = make_user(db, "alice")
    seed_id = await ready_seed(pqueue, owner)
    sub = pqueue.submit_export(owner, seed_id)
    assert mod_row(db, seed_id)["status"] == "pending"

    await wait_until(lambda: pqueue.get_job(sub.job_id, owner)["status"] == "succeeded")
    job = pqueue.get_job(sub.job_id, owner)
    assert (job["kind"], job["error"]) == ("export", None)

    request = fake_runner.exports[0]
    assert request.romfs == romfs_dir
    assert request.rdvgame == pqueue.seeds_dir / owner / seed_id / "seed.rdvgame"

    row = mod_row(db, seed_id)
    assert (row["status"], row["game_version"]) == ("ready", "2.1.0")
    assert (row["file_count"], row["size_bytes"]) == (FAKE_MOD_FILES, FAKE_MOD_BYTES)
    folder = pqueue.seeds_dir / owner / seed_id / "mod"
    assert (folder / "contents/010093801237c000/romfs/packs/system/system.pkg").is_file()
    assert not (folder / "patcher.json").exists()  # layout data never kept in the mod
    assert not any(pqueue.work_dir.iterdir())

    # The seed itself is untouched, and still points at its generation job.
    seeds = SeedStore(db, db.path.parent)
    seed = seeds.get(owner, seed_id)
    assert seed["status"] == "ready"
    assert seed["job_id"] != sub.job_id
    assert (seed["mod_status"], seed["mod_job_id"]) == ("ready", sub.job_id)


async def test_race_seeds_can_be_patched(db, pqueue, pworker) -> None:
    owner = make_user(db, "alice")
    seed_id = await ready_seed(pqueue, owner, race=True)
    sub = pqueue.submit_export(owner, seed_id)
    await wait_until(lambda: pqueue.get_job(sub.job_id, owner)["status"] == "succeeded")


async def test_patching_needs_ready_game_files(db, pqueue, pworker, romfs_dir) -> None:
    owner = make_user(db, "alice")
    seed_id = await ready_seed(pqueue, owner)
    (romfs_dir / "config.ini").unlink()
    with pytest.raises(CannotPatch, match="game files aren't ready"):
        pqueue.submit_export(owner, seed_id)
    assert mod_row(db, seed_id) is None


async def test_game_files_are_checked_again_when_the_job_starts(
    db, pqueue, fake_runner, pworker, romfs_dir
) -> None:
    owner = make_user(db, "alice")
    seed_id = await ready_seed(pqueue, owner)
    fake_runner.release.clear()
    blocker = pqueue.submit_generate(owner, preset_key="starter_preset", seed_number=2)
    await wait_until(lambda: pqueue.get_job(blocker.job_id, owner)["status"] == "running")
    sub = pqueue.submit_export(owner, seed_id)
    (romfs_dir / "config.ini").unlink()  # the game files change while it waits
    fake_runner.release.set()
    await wait_until(lambda: pqueue.get_job(sub.job_id, owner)["status"] == "failed")
    assert "game files aren't ready" in pqueue.get_job(sub.job_id, owner)["error"]
    assert mod_row(db, seed_id)["status"] == "failed"
    assert fake_runner.exports == []


async def test_only_ready_unpatched_seeds_can_be_patched(db, pqueue, fake_runner, pworker) -> None:
    owner = make_user(db, "alice")
    fake_runner.fail_with = "boom"
    failed = pqueue.submit_generate(owner, preset_key="starter_preset", seed_number=1)
    await wait_until(lambda: pqueue.get_job(failed.job_id, owner)["status"] == "failed")
    with pytest.raises(CannotPatch, match="only a generated seed"):
        pqueue.submit_export(owner, failed.seed_id)

    fake_runner.fail_with = None
    seed_id = await ready_seed(pqueue, owner)
    fake_runner.release.clear()
    pqueue.submit_export(owner, seed_id)
    with pytest.raises(CannotPatch, match="already being patched"):
        pqueue.submit_export(owner, seed_id)
    fake_runner.release.set()
    await wait_until(lambda: mod_row(db, seed_id)["status"] == "ready")
    with pytest.raises(CannotPatch, match="already patched"):
        pqueue.submit_export(owner, seed_id)

    with pytest.raises(LookupError):
        pqueue.submit_export(make_user(db, "bob"), seed_id)  # someone else's seed


async def test_a_failed_patch_can_be_retried(db, pqueue, fake_runner, pworker) -> None:
    owner = make_user(db, "alice")
    seed_id = await ready_seed(pqueue, owner)
    fake_runner.fail_with = "Randovania failed (exit code 1): out of memory"
    first = pqueue.submit_export(owner, seed_id)
    await wait_until(lambda: pqueue.get_job(first.job_id, owner)["status"] == "failed")
    assert mod_row(db, seed_id)["status"] == "failed"
    assert "out of memory" in pqueue.get_job(first.job_id, owner)["error"]
    assert SeedStore(db, db.path.parent).get(owner, seed_id)["status"] == "ready"

    fake_runner.fail_with = None
    again = pqueue.submit_export(owner, seed_id)
    await wait_until(lambda: pqueue.get_job(again.job_id, owner)["status"] == "succeeded")
    assert mod_row(db, seed_id)["status"] == "ready"


async def test_cancelling_a_patch(db, pqueue, fake_runner, pworker) -> None:
    owner = make_user(db, "alice")
    seed_id = await ready_seed(pqueue, owner)
    fake_runner.release.clear()
    fake_runner.started.clear()
    sub = pqueue.submit_export(owner, seed_id)
    await fake_runner.started.wait()
    assert pqueue.cancel(sub.job_id, owner)
    await wait_until(lambda: pqueue.get_job(sub.job_id, owner)["status"] == "cancelled")
    assert mod_row(db, seed_id)["status"] == "cancelled"
    assert SeedStore(db, db.path.parent).get(owner, seed_id)["status"] == "ready"
    fake_runner.release.set()


async def test_an_interrupted_patch_is_marked_failed(db, pqueue, fake_runner) -> None:
    owner = make_user(db, "alice")
    worker = asyncio.create_task(pqueue.run_worker())
    seed_id = await ready_seed(pqueue, owner)
    fake_runner.release.clear()
    fake_runner.started.clear()
    sub = pqueue.submit_export(owner, seed_id)
    await fake_runner.started.wait()
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker
    assert pqueue.get_job(sub.job_id, owner)["error"] == INTERRUPTED
    assert mod_row(db, seed_id)["status"] == "failed"
    assert SeedStore(db, db.path.parent).get(owner, seed_id)["status"] == "ready"


async def test_a_seed_being_patched_cannot_be_deleted_or_purged(
    db, pqueue, fake_runner, pworker
) -> None:
    owner = make_user(db, "alice")
    seed_id = await ready_seed(pqueue, owner)
    seeds = SeedStore(db, db.path.parent)
    fake_runner.release.clear()
    pqueue.submit_export(owner, seed_id)
    with pytest.raises(SeedBusy):
        seeds.delete(owner, seed_id)
    with pytest.raises(SeedBusy):
        seeds.delete_mod(owner, seed_id)
    assert seeds.count_purgeable(owner, None) == 0
    assert seeds.purge(owner, None) == (0, 1)
    fake_runner.release.set()
    await wait_until(lambda: mod_row(db, seed_id)["status"] == "ready")

    # Once done: deleting the seed deletes its mod, files and row.
    folder = pqueue.seeds_dir / owner / seed_id / "mod"
    assert folder.is_dir()
    assert seeds.delete(owner, seed_id)
    assert not folder.exists()
    assert mod_row(db, seed_id) is None


async def test_deleting_only_the_mod_keeps_the_seed(db, pqueue, pworker) -> None:
    owner = make_user(db, "alice")
    seed_id = await ready_seed(pqueue, owner)
    sub = pqueue.submit_export(owner, seed_id)
    await wait_until(lambda: pqueue.get_job(sub.job_id, owner)["status"] == "succeeded")
    seeds = SeedStore(db, db.path.parent)
    assert seeds.delete_mod(owner, seed_id)
    assert seeds.get(owner, seed_id)["mod_status"] is None
    assert not (pqueue.seeds_dir / owner / seed_id / "mod").exists()
    assert (pqueue.seeds_dir / owner / seed_id / "seed.rdvgame").is_file()
    assert not seeds.delete_mod(owner, seed_id)


# ---- checking and zipping the patcher's output -----------------------------------------------


def test_finalize_moves_the_mod_and_replaces_an_old_one(tmp_path: Path) -> None:
    dest = tmp_path / "seed" / "mod"
    dest.mkdir(parents=True)
    (dest / "stale").write_text("old")
    out = tmp_path / "out"
    write_fake_mod(out)
    info = mods.finalize_mod(out, dest)
    assert (info.file_count, info.size_bytes) == (FAKE_MOD_FILES, FAKE_MOD_BYTES)
    assert not out.exists()
    assert not (dest / "stale").exists()
    assert sorted(p.name for p in dest.parent.iterdir()) == ["mod"]


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda m: shutil.rmtree(m / "contents"), "for Atmosphere"),
        (lambda m: (m / "surprise.txt").write_text("?"), "unexpected"),
        (lambda m: (m / "contents" / "link").symlink_to("/etc/passwd"), "plain files"),
        (lambda m: (m / "exefs_patches" / "dir").symlink_to("/etc", True), "plain files"),
    ],
)
def test_unusable_output_is_refused(tmp_path: Path, damage, message) -> None:
    out = tmp_path / "out"
    write_fake_mod(out)
    damage(out)
    with pytest.raises(mods.ModError, match=message):
        mods.finalize_mod(out, tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


def test_the_zip_is_extracted_at_the_sd_card_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    write_fake_mod(out)
    mods.finalize_mod(out, tmp_path / "mod")
    data = b"".join(mods.iter_mod_zip(tmp_path / "mod"))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert names == sorted(names)
        assert all(n.startswith("atmosphere/") for n in names)
        assert "atmosphere/contents/010093801237c000/romfs/packs/system/system.pkg" in names
        assert "atmosphere/exefs_patches/DreadRandovania/ABCDEF.ips" in names
        assert not any("patcher.json" in n for n in names)
        assert archive.read("atmosphere/exefs_patches/DreadRandovania/ABCDEF.ips") == b"IPS"


# ---- the real runner, against a stand-in CLI -------------------------------------------------

FAKE_CLI = textwrap.dedent(
    """
    import json, pathlib, sys
    mode, args = sys.argv[1], sys.argv[2:]
    log = pathlib.Path(sys.argv[0]).with_name("calls.txt")
    with log.open("a") as f:
        f.write(json.dumps(args) + "\\n")
    if args[:2] == ["layout", "patcher-data"]:
        out = pathlib.Path(args[args.index("--output") + 1])
        if mode != "no-data":
            out.write_text("{}")
    elif args[:2] == ["layout", "export-game"]:
        if mode == "crash":
            print("RuntimeError: bad romfs", file=sys.stderr)
            sys.exit(2)
        out = pathlib.Path(args[args.index("--output-path") + 1])
        (out / "contents").mkdir(parents=True)
        print("[ 50%] Patching", flush=True)
    """
)


@pytest.fixture
def fake_cli(tmp_path: Path):
    script = tmp_path / "cli" / "fake_randovania.py"
    script.parent.mkdir()
    script.write_text(FAKE_CLI)
    return script, lambda mode: RandovaniaRunner(command=[sys.executable, str(script), mode])


REQ = ExportRequest(rdvgame=Path("/data/seeds/a/b/seed.rdvgame"), romfs=Path("/data/romfs"))


async def test_runner_exports_with_the_documented_commands(tmp_path: Path, fake_cli) -> None:
    script, make = fake_cli
    lines: list[str] = []
    result = await make("ok").export(REQ, tmp_path, lines.append)
    assert result.mod == tmp_path / "mod"
    calls = [json.loads(c) for c in (script.parent / "calls.txt").read_text().splitlines()]
    assert calls[0] == [
        "layout", "patcher-data", "--layout-file", str(REQ.rdvgame),
        "--output", str(tmp_path / "patcher-data.json"),
    ]  # fmt: skip
    assert calls[1] == [
        "layout", "export-game", "--patcher-data", str(tmp_path / "patcher-data.json"), "dread",
        "--input-path", "/data/romfs", "--output-path", str(tmp_path / "mod"),
        "--target-platform", "atmosphere", "--use-exlaunch",
    ]  # fmt: skip
    assert lines == ["Preparing the patch", "[ 50%] Patching"]


async def test_runner_export_failures(tmp_path: Path, fake_cli) -> None:
    _script, make = fake_cli
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    with pytest.raises(RunnerError, match="bad romfs"):
        await make("crash").export(REQ, tmp_path / "a", lambda _line: None)
    with pytest.raises(RunnerError, match="patcher data"):
        await make("no-data").export(REQ, tmp_path / "b", lambda _line: None)


async def test_runner_export_timeout(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text("import time; time.sleep(30)")
    runner = RandovaniaRunner(command=[sys.executable, str(script)], export_timeout=0.5)
    with pytest.raises(RunnerError, match="timed out after"):
        await runner.export(REQ, tmp_path, lambda _l: None)


# ---- the migration ---------------------------------------------------------------------------


def test_migration_keeps_existing_jobs_and_allows_exports(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "d.db")
    monkeypatch.setattr(db, "_migrations", [m for m in db._migrations if m[0] <= 3])
    db.migrate()
    owner = "alice"
    with db.connect() as conn:  # the schema of version 3: no roles yet
        conn.execute("INSERT INTO users (id, created_at) VALUES (?, 'now')", (owner,))
        conn.execute(
            "INSERT INTO seeds (id, owner_id, status, source, preset_key, randovania_version,"
            " created_at) VALUES ('s1', ?, 'ready', 'preset', 'starter_preset', '12.0.0', 'now')",
            (owner,),
        )
        conn.execute(
            "INSERT INTO jobs (id, owner_id, seed_id, kind, status, created_at)"
            " VALUES ('j1', ?, 's1', 'generate', 'succeeded', 'now')",
            (owner,),
        )
    monkeypatch.undo()
    assert db.migrate() == db.latest_version
    with db.connect() as conn:
        assert conn.execute("SELECT kind, status FROM jobs WHERE id = 'j1'").fetchone()[:] == (
            "generate", "succeeded",
        )  # fmt: skip
        conn.execute(
            "INSERT INTO jobs (id, owner_id, seed_id, kind, status, created_at)"
            " VALUES ('j2', ?, 's1', 'export', 'queued', 'now')",
            (owner,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs (id, owner_id, seed_id, kind, status, created_at)"
                " VALUES ('j3', ?, 's1', 'nonsense', 'queued', 'now')",
                (owner,),
            )
        conn.execute("DELETE FROM seeds WHERE id = 's1'")  # cascades still work after the rebuild
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


# ---- API and UI ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, fake_runner: FakeRunner, romfs_dir: Path) -> Iterator[TestClient]:
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        runner=fake_runner,
        romfs=RomfsInspector(romfs_dir, identify=fake_identify),
    )
    with TestClient(app, base_url=ORIGIN) as c:
        token = auth.create_setup_token(app.state.db)
        r = c.post(
            "/api/v1/setup",
            json={"token": token, "username": "samus", "password": "correct horse battery",
                  "public_url": ORIGIN},
            headers=H,
        )  # fmt: skip
        assert r.status_code == 201
        yield c


def api_ready_seed(client: TestClient) -> str:
    r = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H)
    assert wait_for_job(client, r.json()["job_id"])["status"] == "succeeded"
    return r.json()["seed_id"]


def test_api_patch_download_and_delete(client: TestClient) -> None:
    seed_id = api_ready_seed(client)
    assert client.get(f"/api/v1/seeds/{seed_id}").json()["mod"] is None
    assert client.get(f"/api/v1/seeds/{seed_id}/mod.zip").status_code == 409

    r = client.post(f"/api/v1/seeds/{seed_id}/mod", headers=H)
    assert r.status_code == 202, r.text
    job = wait_for_job(client, r.json()["job_id"])
    assert (job["kind"], job["status"]) == ("export", "succeeded")

    mod = client.get(f"/api/v1/seeds/{seed_id}").json()["mod"]
    assert mod["status"] == "ready"
    assert (mod["game_version"], mod["file_count"], mod["size_bytes"]) == (
        "2.1.0", FAKE_MOD_FILES, FAKE_MOD_BYTES,
    )  # fmt: skip
    assert mod["job_id"] == r.json()["job_id"]

    again = client.post(f"/api/v1/seeds/{seed_id}/mod", headers=H)
    assert again.status_code == 409
    assert "already patched" in again.json()["detail"]

    z = client.get(f"/api/v1/seeds/{seed_id}/mod.zip")
    assert z.status_code == 200
    assert z.headers["content-type"] == "application/zip"
    assert z.headers["content-disposition"] == 'attachment; filename="dreadovania-HASH1234.zip"'
    with zipfile.ZipFile(io.BytesIO(z.content)) as archive:
        assert all(n.startswith("atmosphere/") for n in archive.namelist())

    assert client.delete(f"/api/v1/seeds/{seed_id}/mod", headers=H).status_code == 204
    assert client.get(f"/api/v1/seeds/{seed_id}").json()["mod"] is None
    assert client.delete(f"/api/v1/seeds/{seed_id}/mod", headers=H).status_code == 404


def test_api_refuses_without_game_files(client: TestClient, romfs_dir: Path) -> None:
    seed_id = api_ready_seed(client)
    (romfs_dir / "config.ini").unlink()
    r = client.post(f"/api/v1/seeds/{seed_id}/mod", headers=H)
    assert r.status_code == 409
    assert "game files aren't ready" in r.json()["detail"]


def test_api_patching_is_owner_scoped_and_protected(client: TestClient) -> None:
    seed_id = api_ready_seed(client)
    missing = "00000000-0000-4000-8000-000000000000"
    assert client.post(f"/api/v1/seeds/{missing}/mod", headers=H).status_code == 404
    assert client.post("/api/v1/seeds/not-a-uuid/mod", headers=H).status_code == 404
    assert client.get(f"/api/v1/seeds/{missing}/mod.zip").status_code == 404
    assert client.post(f"/api/v1/seeds/{seed_id}/mod").status_code == 403  # no Origin: CSRF
    client.cookies.clear()
    assert client.post(f"/api/v1/seeds/{seed_id}/mod", headers=H).status_code == 401
    assert client.get(f"/api/v1/seeds/{seed_id}/mod.zip").status_code == 401


def test_seed_page_offers_patching(client: TestClient, romfs_dir: Path) -> None:
    seed_id = api_ready_seed(client)
    page = client.get(f"/seeds/{seed_id}").text
    assert "Patch for Switch" in page
    assert "Dread 2.1.0" in page

    r = client.post(f"/seeds/{seed_id}/patch", headers={**H, "HX-Request": "true"})
    assert r.status_code in (200, 204)
    seed = client.get(f"/api/v1/seeds/{seed_id}").json()
    wait_for_job(client, seed["mod"]["job_id"])
    page = client.get(f"/seeds/{seed_id}").text
    assert f"/api/v1/seeds/{seed_id}/mod.zip" in page
    assert "root of your SD card" in page
    assert "patched" in client.get("/").text

    client.post(f"/seeds/{seed_id}/mod/delete", headers={**H, "HX-Request": "true"})
    (romfs_dir / "config.ini").unlink()
    page = client.get(f"/seeds/{seed_id}").text
    assert "Set up game files" in page
    assert str(romfs_dir) not in page
