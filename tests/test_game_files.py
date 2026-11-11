# SPDX-License-Identifier: GPL-3.0-or-later
"""Each account's own game files (ADR 0020 §3): upload, check and patch with your own only."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FakeRunner
from fastapi.testclient import TestClient
from test_accounts import as_member
from test_api import ORIGIN, H, use_session, wait_for_job
from test_romfs import fake_identify
from test_romfs_upload import CHUNK, _install_and_wait, _send, romfs_zip

from dreadovania import auth
from dreadovania.app import create_app
from dreadovania.config import Settings
from dreadovania.romfs import RomfsInspector


@pytest.fixture
def client(tmp_path: Path, fake_runner: FakeRunner) -> Iterator[TestClient]:
    """Signed in as the admin, with no game files anywhere yet."""
    data = tmp_path / "data"
    app = create_app(
        Settings(data_dir=data, romfs_upload_chunk_bytes=CHUNK),
        runner=fake_runner,
        romfs=RomfsInspector(data / "romfs", identify=fake_identify),
    )
    with TestClient(app, base_url=ORIGIN) as c:
        r = c.post("/api/v1/setup", headers=H, json={
            "token": auth.create_setup_token(app.state.db), "username": "samus",
            "password": "correct horse battery", "public_url": ORIGIN})  # fmt: skip
        assert r.status_code == 201
        yield c


def member_id(client: TestClient, username: str = "ridley") -> str:
    with client.app.state.db.connect() as conn:
        return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]


def ready_seed(client: TestClient) -> str:
    r = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H)
    assert wait_for_job(client, r.json()["job_id"])["status"] == "succeeded"
    return r.json()["seed_id"]


def patch(client: TestClient, seed_id: str):
    return client.post(f"/api/v1/seeds/{seed_id}/mod", headers=H)


def test_a_member_uploads_their_own_game_files(client: TestClient, tmp_path: Path) -> None:
    admin_cookie = as_member(client)
    assert client.get("/api/v1/romfs").json()["state"] == "missing"
    assert client.get("/api/v1/romfs").json()["upload_allowed"] is True
    upload = _install_and_wait(client, _send(client, romfs_zip(toc=b"toc-1.0.0"), "mine"))
    assert upload["state"] == "installed"
    assert client.get("/api/v1/romfs").json()["version"] == "1.0.0"
    mine = tmp_path / "data" / "accounts" / member_id(client) / "romfs"
    assert (mine / "system" / "files.toc").is_file()

    use_session(client, admin_cookie)  # the admin's are untouched, and still missing
    assert client.get("/api/v1/romfs").json()["state"] == "missing"
    assert not (tmp_path / "data" / "romfs").exists()


def test_nobody_sees_or_touches_another_accounts_upload(client: TestClient) -> None:
    admin_cookie = as_member(client)
    member_cookie = client.cookies["dreadovania_session"]
    upload_id = _send(client, romfs_zip(), "mine")  # received, not installed

    use_session(client, admin_cookie)
    assert client.get("/api/v1/romfs/upload").status_code == 404
    for method, path in (("POST", f"/api/v1/romfs/upload/{upload_id}/install"),
                         ("DELETE", f"/api/v1/romfs/upload/{upload_id}"),
                         ("PATCH", f"/api/v1/romfs/upload/{upload_id}")):  # fmt: skip
        r = client.request(method, path, content=b"x", headers=H | {"Upload-Offset": "0"})
        assert r.status_code == 404, (method, path)

    use_session(client, member_cookie)
    assert client.get("/api/v1/romfs/upload").json()["state"] == "received"


def test_each_account_patches_with_its_own_game_files(
    client: TestClient, fake_runner: FakeRunner, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    assert _install_and_wait(client, _send(client, romfs_zip(), "admin"))["state"] == "installed"
    admin_seed = ready_seed(client)
    admin_cookie = as_member(client)

    member_seed = ready_seed(client)
    refused = patch(
        client, member_seed
    )  # the admin's files are ready, but they aren't the member's
    assert refused.status_code == 409
    assert "game files aren't ready" in refused.json()["detail"]
    assert _install_and_wait(client, _send(client, romfs_zip(), "member"))["state"] == "installed"
    r = patch(client, member_seed)
    assert r.status_code == 202, r.text
    assert wait_for_job(client, r.json()["job_id"])["status"] == "succeeded"
    assert fake_runner.exports[-1].romfs == data / "accounts" / member_id(client) / "romfs"

    use_session(client, admin_cookie)
    r = patch(client, admin_seed)
    assert wait_for_job(client, r.json()["job_id"])["status"] == "succeeded"
    assert fake_runner.exports[-1].romfs == data / "romfs"


def test_a_members_patch_doesnt_block_the_admins_install(
    client: TestClient, fake_runner: FakeRunner
) -> None:
    admin_cookie = as_member(client)
    member_cookie = client.cookies["dreadovania_session"]
    assert _install_and_wait(client, _send(client, romfs_zip(), "member"))["state"] == "installed"
    seed = ready_seed(client)
    fake_runner.release.clear()
    job = patch(client, seed).json()["job_id"]
    deadline = time.monotonic() + 10
    while client.get(f"/api/v1/jobs/{job}").json()["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    # The member can't replace their own files mid-patch...
    blocked = _send(client, romfs_zip(toc=b"toc-1.0.0"), "member-2")
    r = client.post(f"/api/v1/romfs/upload/{blocked}/install", headers=H)
    assert r.status_code == 409 and "being patched" in r.json()["detail"]
    # ...but the admin can replace theirs.
    use_session(client, admin_cookie)
    assert _install_and_wait(client, _send(client, romfs_zip(), "admin"))["state"] == "installed"
    assert client.get(f"/api/v1/jobs/{job}").status_code == 404  # and can't see the member's job
    fake_runner.release.set()
    use_session(client, member_cookie)
    assert wait_for_job(client, job)["status"] == "succeeded"


def test_a_mounted_folder_is_only_the_admins(client: TestClient, monkeypatch) -> None:
    admin_root = client.app.state.romfs.root
    real_ismount = __import__("os").path.ismount
    monkeypatch.setattr("os.path.ismount", lambda p: Path(p) == admin_root or real_ismount(p))
    assert client.get("/api/v1/romfs").json()["upload_allowed"] is False  # mounted: the admin's
    as_member(client)
    assert client.get("/api/v1/romfs").json()["upload_allowed"] is True  # members upload theirs


def test_removing_a_member_deletes_their_game_files(client: TestClient, tmp_path: Path) -> None:
    admin_cookie = as_member(client)
    assert _install_and_wait(client, _send(client, romfs_zip(), "member"))["state"] == "installed"
    folder = tmp_path / "data" / "accounts" / member_id(client)
    assert folder.is_dir()
    use_session(client, admin_cookie)
    sizes = {a["username"]: a["game_files_bytes"] for a in client.get("/api/v1/accounts").json()}
    assert sizes["ridley"] > 0 and sizes["samus"] == 0  # the admin sees each account's disk use
    assert "game files" in client.get("/account").text
    assert client.delete(f"/api/v1/accounts/{member_id(client)}", headers=H).status_code == 204
    assert not folder.exists()


def test_the_game_files_page_is_the_accounts_own(client: TestClient) -> None:
    as_member(client)
    page = client.get("/game-files").text
    assert "No game files yet" in page
    assert "invited accounts" not in page
