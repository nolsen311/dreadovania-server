# SPDX-License-Identifier: GPL-3.0-or-later
"""The generation API over HTTP (ADR 0011 §3), with the fake runner unless noted."""

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeRunner
from fastapi.testclient import TestClient

from dreadovania import auth
from dreadovania.app import create_app
from dreadovania.config import Settings
from dreadovania.jobs import new_id
from dreadovania.presets import bundled_dread_presets

ORIGIN = "https://dread.test"
H = {"Origin": ORIGIN}
STARTER = bundled_dread_presets()["starter_preset"].path


def _client(tmp_path: Path, runner: FakeRunner | None) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path / "data"), runner=runner)
    with TestClient(app, base_url=ORIGIN) as client:
        token = auth.create_setup_token(app.state.db)
        response = client.post(
            "/api/v1/setup",
            json={"token": token, "username": "samus", "password": "correct horse battery",
                  "public_url": ORIGIN},
            headers=H,
        )  # fmt: skip
        assert response.status_code == 201
        yield client


@pytest.fixture
def client(tmp_path: Path, fake_runner: FakeRunner) -> Iterator[TestClient]:
    yield from _client(tmp_path, fake_runner)


def wait_for_job(client: TestClient, job_id: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} didn't finish")


def generate(client: TestClient, **body) -> dict:
    response = client.post("/api/v1/seeds", json={"preset_id": "starter_preset", **body}, headers=H)
    assert response.status_code == 202, response.text
    return response.json()


def second_user(client: TestClient) -> str:
    """Create another account and return a session token for it."""
    db = client.app.state.db
    user_id = new_id()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users (id, created_at, username) VALUES (?, ?, 'ridley')",
            (user_id, datetime.now(UTC).isoformat()),
        )
    return auth.create_session(db, user_id)


def use_session(client: TestClient, token: str) -> None:
    client.cookies.set("dreadovania_session", token, domain="dread.test")


# ---- presets -----------------------------------------------------------------------------


def test_bundled_presets_are_listed(client) -> None:
    presets = client.get("/api/v1/presets").json()
    starter = next(p for p in presets if p["id"] == "starter_preset")
    assert starter == {"id": "starter_preset", "name": "Starter Preset",
                       "description": starter["description"], "builtin": True}  # fmt: skip


def test_import_get_list_and_delete_a_preset(client) -> None:
    response = client.post("/api/v1/presets", content=STARTER.read_bytes(), headers=H)
    assert response.status_code == 201, response.text
    preset = response.json()
    assert preset["id"].startswith("user:") and preset["builtin"] is False
    assert preset["name"] == "Starter Preset"
    assert preset in client.get("/api/v1/presets").json()
    assert client.get(f"/api/v1/presets/{preset['id']}").json() == preset

    assert client.delete(f"/api/v1/presets/{preset['id']}", headers=H).status_code == 204
    assert client.get(f"/api/v1/presets/{preset['id']}").status_code == 404


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"not json", "not valid JSON"),
        (b"[1, 2]", "isn't a Randovania preset"),
        (json.dumps({"game": "prime1", "name": "x", "configuration": {}}).encode(), "Dread"),
        (json.dumps({"game": "dread", "name": "", "configuration": {}}).encode(), "name"),
        (json.dumps({"game": "dread", "name": "x" * 101, "configuration": {}}).encode(), "name"),
    ],
)
def test_invalid_presets_are_rejected(client, body: bytes, detail: str) -> None:
    response = client.post("/api/v1/presets", content=body, headers=H)
    assert response.status_code == 400
    assert detail in response.json()["detail"]


def test_oversized_preset_is_rejected(client) -> None:
    response = client.post("/api/v1/presets", content=b" " * (1024 * 1024 + 1), headers=H)
    assert response.status_code == 413


def test_bundled_presets_cannot_be_deleted(client) -> None:
    assert client.delete("/api/v1/presets/starter_preset", headers=H).status_code == 404


# ---- generating ------------------------------------------------------------------------------


def test_generate_download_and_reveal_spoiler(client) -> None:
    sub = generate(client, seed_number=7)
    assert wait_for_job(client, sub["job_id"])["status"] == "succeeded"

    seed = client.get(f"/api/v1/seeds/{sub['seed_id']}").json()
    assert seed["status"] == "ready"
    assert (seed["preset_id"], seed["preset_name"]) == ("starter_preset", "Starter Preset")
    assert (seed["seed_number"], seed["race"], seed["job_id"]) == (7, False, sub["job_id"])
    assert seed["permalink"] == "FAKEPERMALINK" and seed["seed_hash"] == "HASH1234"

    download = client.get(f"/api/v1/seeds/{sub['seed_id']}/rdvgame")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    assert "dreadovania-HASH1234.rdvgame" in download.headers["content-disposition"]

    spoiler = client.get(f"/api/v1/seeds/{sub['seed_id']}/spoiler").json()
    assert spoiler["starting_location"] == {
        "region": "Artaria", "area": "Intro Room", "node": "Start Point"
    }  # fmt: skip
    assert spoiler["starting_items"] == ["Missiles", "Pulse Radar"]
    assert spoiler["locations"] == [{
        "region": "Artaria", "area": "Arbitrary Enky Room",
        "location": "Pickup (Missile Tank)", "pickup": "Power Bomb Tank",
    }]  # fmt: skip


def test_race_seeds_have_no_spoiler(client) -> None:
    sub = generate(client, race=True)
    wait_for_job(client, sub["job_id"])
    response = client.get(f"/api/v1/seeds/{sub['seed_id']}/spoiler")
    assert response.status_code == 409
    assert "race" in response.json()["detail"]
    assert "game_modifications" not in client.get(f"/api/v1/seeds/{sub['seed_id']}/rdvgame").json()


def test_generate_from_an_imported_preset(client, fake_runner) -> None:
    preset = client.post("/api/v1/presets", content=STARTER.read_bytes(), headers=H).json()
    sub = generate(client, preset_id=preset["id"])
    assert wait_for_job(client, sub["job_id"])["status"] == "succeeded"
    assert fake_runner.requests[-1].preset_file.name == "preset.rdvpreset"  # written per job
    assert client.get(f"/api/v1/seeds/{sub['seed_id']}").json()["preset_name"] == "Starter Preset"


def test_deleting_a_preset_before_its_job_runs_fails_the_job_cleanly(client, fake_runner) -> None:
    fake_runner.release.clear()
    blocker = generate(client)  # occupies the worker
    preset = client.post("/api/v1/presets", content=STARTER.read_bytes(), headers=H).json()
    waiting = generate(client, preset_id=preset["id"])
    client.delete(f"/api/v1/presets/{preset['id']}", headers=H)
    fake_runner.release.set()
    assert wait_for_job(client, blocker["job_id"])["status"] == "succeeded"
    job = wait_for_job(client, waiting["job_id"])
    assert (job["status"], job["error"]) == (
        "failed", "the preset was deleted before this seed could be generated"
    )  # fmt: skip


def test_generate_from_a_permalink(client, fake_runner) -> None:
    sub = generate(client, preset_id=None, permalink="DWT2Y0yRC4rILFDaWMyRyTb8AAHo")
    wait_for_job(client, sub["job_id"])
    assert fake_runner.requests[-1].permalink == "DWT2Y0yRC4rILFDaWMyRyTb8AAHo"


@pytest.mark.parametrize(
    "body",
    [
        {"preset_id": "nope"},
        {"preset_id": "user:00000000-0000-0000-0000-000000000000"},
        {"preset_id": "starter_preset", "seed_number": -1},
        {"preset_id": None, "permalink": "bad permalink!"},
        {"preset_id": None},
    ],
)
def test_invalid_generation_requests(client, body: dict) -> None:
    response = client.post("/api/v1/seeds", json=body, headers=H)
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"


def test_queue_limit(client, fake_runner) -> None:
    fake_runner.release.clear()
    for _ in range(5):
        generate(client)
    response = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H)
    assert response.status_code == 429
    fake_runner.release.set()


# ---- jobs ---------------------------------------------------------------------------------


def test_live_progress_over_sse(client) -> None:
    sub = generate(client)
    events = []
    with client.stream("GET", f"/api/v1/jobs/{sub['job_id']}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
    assert events, "no events received"
    assert events[-1]["status"] == "succeeded"  # the stream ends when the job does
    assert all(e["id"] == sub["job_id"] for e in events)


def test_cancel_a_running_job(client, fake_runner) -> None:
    fake_runner.release.clear()
    sub = generate(client)
    assert client.delete(f"/api/v1/jobs/{sub['job_id']}", headers=H).status_code == 204
    assert wait_for_job(client, sub["job_id"])["status"] == "cancelled"
    assert client.delete(f"/api/v1/jobs/{sub['job_id']}", headers=H).status_code == 409
    fake_runner.release.set()


MALFORMED = [
    "/api/v1/jobs/x",
    "/api/v1/seeds/../../etc/passwd",
    "/api/v1/seeds/not-a-uuid/rdvgame",
    "/api/v1/presets/user:x",
]


@pytest.mark.parametrize("path", MALFORMED)
def test_malformed_ids_are_not_found(client, path: str) -> None:
    assert client.get(path).status_code == 404


# ---- deleting and purging ---------------------------------------------------------------------


def test_delete_a_seed(client, fake_runner) -> None:
    fake_runner.release.clear()
    sub = generate(client)
    assert client.delete(f"/api/v1/seeds/{sub['seed_id']}", headers=H).status_code == 409
    fake_runner.release.set()
    wait_for_job(client, sub["job_id"])
    seed_file = client.app.state.seeds.seeds_dir.rglob("seed.rdvgame")
    assert any(seed_file)
    assert client.delete(f"/api/v1/seeds/{sub['seed_id']}", headers=H).status_code == 204
    assert client.get(f"/api/v1/seeds/{sub['seed_id']}").status_code == 404
    assert not any(client.app.state.seeds.seeds_dir.rglob("seed.rdvgame"))


def test_purge_needs_an_explicit_scope(client) -> None:
    for body in (
        {},
        {"scope": "everything"},
        {"scope": "older_than"},
        {"scope": "older_than", "days": 0},
    ):
        assert client.post("/api/v1/seeds/purge", json=body, headers=H).status_code == 422, body


def test_purge_older_than_and_all(client) -> None:
    subs = [generate(client) for _ in range(3)]
    for s in subs:
        wait_for_job(client, s["job_id"])
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="milliseconds")
    with client.app.state.db.connect() as conn:
        conn.execute("UPDATE seeds SET created_at = ? WHERE id = ?", (old, subs[0]["seed_id"]))

    response = client.post(
        "/api/v1/seeds/purge", json={"scope": "older_than", "days": 30}, headers=H
    )
    assert response.json() == {"deleted": 1, "skipped_in_progress": 0}
    assert len(client.get("/api/v1/seeds").json()) == 2

    response = client.post("/api/v1/seeds/purge", json={"scope": "all"}, headers=H)
    assert response.json() == {"deleted": 2, "skipped_in_progress": 0}
    assert client.get("/api/v1/seeds").json() == []
    assert not any(client.app.state.seeds.seeds_dir.rglob("seed.rdvgame"))


# ---- ownership -----------------------------------------------------------------------------


def test_users_cannot_see_or_touch_each_others_things(client) -> None:
    preset = client.post("/api/v1/presets", content=STARTER.read_bytes(), headers=H).json()
    sub = generate(client)
    wait_for_job(client, sub["job_id"])

    use_session(client, second_user(client))
    assert client.get("/api/v1/seeds").json() == []
    assert preset not in client.get("/api/v1/presets").json()
    seed, job = f"/api/v1/seeds/{sub['seed_id']}", f"/api/v1/jobs/{sub['job_id']}"
    others = [seed, f"{seed}/rdvgame", f"{seed}/spoiler", job, f"{job}/events",
              f"/api/v1/presets/{preset['id']}"]  # fmt: skip
    for path in others:
        assert client.get(path).status_code == 404, path
    assert client.delete(f"/api/v1/seeds/{sub['seed_id']}", headers=H).status_code == 404
    assert client.delete(f"/api/v1/presets/{preset['id']}", headers=H).status_code == 404
    assert client.delete(f"/api/v1/jobs/{sub['job_id']}", headers=H).status_code == 404
    assert (
        client.post("/api/v1/seeds/purge", json={"scope": "all"}, headers=H).json()["deleted"] == 0
    )
    with pytest.raises(AssertionError):  # cannot generate from someone else's preset
        generate(client, preset_id=preset["id"])


def test_changes_need_a_same_origin_request(client) -> None:
    assert client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}).status_code == 403
    response = client.post(
        "/api/v1/seeds/purge", json={"scope": "all"}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


# ---- real Randovania, end to end (slow) ----------------------------------------------------


def test_real_randovania_end_to_end_over_http(tmp_path: Path) -> None:
    for client in _client(tmp_path, runner=None):
        sub = generate(client, seed_number=12345)
        job = wait_for_job(client, sub["job_id"], timeout=300)
        assert job["status"] == "succeeded", job["error"]
        seed = client.get(f"/api/v1/seeds/{sub['seed_id']}").json()
        assert seed["permalink"] and seed["seed_hash"]
        assert client.get(f"/api/v1/seeds/{sub['seed_id']}/rdvgame").json()["info"]["has_spoiler"]
        spoiler = client.get(f"/api/v1/seeds/{sub['seed_id']}/spoiler").json()
        assert len(spoiler["locations"]) > 100  # a real Dread seed places every pickup
        # The real file's formats are parsed, not just passed through.
        assert set(spoiler["starting_location"]) == {"region", "area", "node"}
        assert spoiler["starting_items"] and all(
            isinstance(i, str) for i in spoiler["starting_items"]
        )
