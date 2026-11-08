# SPDX-License-Identifier: GPL-3.0-or-later
"""Sending a patched seed to a Switch over FTP (ADR 0016, Phase 3 PR 2).

The Switch is a local FTP server (pyftpdlib, the ``ftpd`` fixture) serving a
temporary folder laid out like an SD card. No console or game data is used.
"""

import ftplib
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import write_fake_mod
from fastapi.testclient import TestClient
from test_api import ORIGIN, H, wait_for_job
from test_romfs import fake_identify, make_romfs

from dreadovania import auth
from dreadovania.app import create_app
from dreadovania.config import Settings
from dreadovania.push import SD_MANIFEST, Cancelled, Conflict, PushOptions, push
from dreadovania.romfs import RomfsInspector

DREAD = "atmosphere/contents/010093801237c000"


def connect(server: dict) -> ftplib.FTP:
    ftp = ftplib.FTP(timeout=5)  # noqa: S321 (the stand-in Switch speaks FTP)
    ftp.connect(server["host"], server["port"])
    ftp.login()
    return ftp


def make_mod(path: Path, files: dict[str, bytes] | None = None) -> Path:
    if files is None:
        write_fake_mod(path)
        (path / "patcher.json").unlink()
    else:
        for name, data in files.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(data)
    return path


def send(server: dict, mod: Path, previous: list[str] | None = None, **options) -> dict:
    """Run a push; return what it recorded along the way."""
    seen: dict = {"planned": None, "progress": []}
    with connect(server) as ftp:
        seen["files"] = push(
            ftp, mod, seed_id="seed-1", previous=previous or [],
            options=PushOptions(**options),
            record_planned=lambda files: seen.update(planned=files),
            on_progress=lambda p: seen["progress"].append(p),
            cancelled=lambda: False,
        )  # fmt: skip
    return seen


def sd_manifest(root: Path) -> dict:
    return json.loads((root / SD_MANIFEST.lstrip("/")).read_text())


# ---- installing ------------------------------------------------------------------------------


def test_a_first_send_installs_the_seed(ftpd: dict, tmp_path: Path) -> None:
    sd = ftpd["root"]
    seen = send(ftpd, make_mod(tmp_path / "mod"))
    assert (sd / DREAD / "romfs/packs/system/system.pkg").read_bytes() == b"P" * 1000
    assert (sd / "atmosphere/exefs_patches/DreadRandovania/ABCDEF.ips").read_bytes() == b"IPS"
    assert seen["files"] == sorted(seen["files"])
    assert "/atmosphere/contents/010093801237c000/exefs/main.npdm" in seen["files"]
    manifest = sd_manifest(sd)
    assert (manifest["state"], manifest["files"], manifest["seed_id"]) == (
        "installed", seen["files"], "seed-1",
    )  # fmt: skip
    last = seen["progress"][-1]
    assert (last.files_done, last.bytes_done) == (last.files_total, last.bytes_total)


def test_the_next_send_removes_the_previous_seed_but_keeps_other_mods(
    ftpd: dict, tmp_path: Path
) -> None:
    sd = ftpd["root"]
    first = send(ftpd, make_mod(tmp_path / "a", {
        "contents/010093801237c000/romfs/a.bin": b"a",
        "contents/010093801237c000/romfs/deep/only-a.bin": b"a",
        "exefs_patches/DreadRandovania/x.ips": b"x",
    }))  # fmt: skip
    # Another mod's files, in Dread's folder and elsewhere.
    other = sd / DREAD / "romfs/suit/colour.bfres"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"suit")
    elsewhere = sd / "atmosphere/contents/0100000000001000/romfs/x"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"x")

    second = send(ftpd, make_mod(tmp_path / "b", {"contents/010093801237c000/romfs/a.bin": b"b"}),
                  previous=first["files"])  # fmt: skip
    assert (sd / DREAD / "romfs/a.bin").read_bytes() == b"b"
    assert not (sd / DREAD / "romfs/deep").exists()  # emptied, so removed
    assert not (sd / "atmosphere/exefs_patches/DreadRandovania").exists()
    assert other.read_bytes() == b"suit"  # another mod: kept
    assert elsewhere.read_bytes() == b"x"
    assert second["files"] == ["/atmosphere/contents/010093801237c000/romfs/a.bin"]


def test_the_sd_manifest_is_used_when_the_server_has_none(ftpd: dict, tmp_path: Path) -> None:
    sd = ftpd["root"]
    send(ftpd, make_mod(tmp_path / "a", {"contents/010093801237c000/romfs/old.bin": b"a"}))
    # A server that has never seen this Switch (previous=[]) still cleans up.
    send(ftpd, make_mod(tmp_path / "b", {"contents/010093801237c000/romfs/new.bin": b"b"}))
    assert not (sd / DREAD / "romfs/old.bin").exists()


def test_a_tampered_manifest_never_deletes_outside_dreads_folders(
    ftpd: dict, tmp_path: Path
) -> None:
    sd = ftpd["root"]
    victim = sd / "atmosphere/config/system_settings.ini"
    victim.parent.mkdir(parents=True)
    victim.write_text("keep me")
    (sd / "config/dreadovania").mkdir(parents=True)
    (sd / SD_MANIFEST.lstrip("/")).write_text(json.dumps({"files": [
        "/atmosphere/config/system_settings.ini",
        "/atmosphere/contents/010093801237c000/../../config/system_settings.ini",
    ]}))  # fmt: skip
    send(ftpd, make_mod(tmp_path / "a"), previous=["/atmosphere/config/system_settings.ini"])
    assert victim.read_text() == "keep me"


# ---- other mods' files -----------------------------------------------------------------------


def test_replacing_another_mods_file_stops_the_send(ftpd: dict, tmp_path: Path) -> None:
    sd = ftpd["root"]
    theirs = sd / DREAD / "romfs/packs/system/system.pkg"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"another mod")
    with pytest.raises(Conflict) as raised:
        send(ftpd, make_mod(tmp_path / "mod"))
    assert raised.value.files == [
        "/atmosphere/contents/010093801237c000/romfs/packs/system/system.pkg"
    ]
    assert "Replace other mods' files" in str(raised.value)
    assert theirs.read_bytes() == b"another mod"  # untouched
    assert not (sd / SD_MANIFEST.lstrip("/")).exists()  # nothing recorded: nothing changed


def test_overwrite_replaces_them_and_then_owns_them(ftpd: dict, tmp_path: Path) -> None:
    sd = ftpd["root"]
    theirs = sd / DREAD / "romfs/packs/system/system.pkg"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"another mod")
    first = send(ftpd, make_mod(tmp_path / "mod"), overwrite=True)
    assert theirs.read_bytes() == b"P" * 1000
    # Now Dreadovania's: the next seed removes it without a conflict.
    send(ftpd, make_mod(tmp_path / "b", {"contents/010093801237c000/romfs/a.bin": b"b"}),
         previous=first["files"])  # fmt: skip
    assert not theirs.exists()


def test_clear_empties_dreads_folders_only(ftpd: dict, tmp_path: Path) -> None:
    sd = ftpd["root"]
    theirs = sd / DREAD / "romfs/suit/colour.bfres"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"suit")
    elsewhere = sd / "atmosphere/contents/0100000000001000/romfs/x"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"x")
    send(ftpd, make_mod(tmp_path / "mod"), clear=True)
    assert not theirs.exists()
    assert elsewhere.read_bytes() == b"x"
    assert (sd / DREAD / "romfs/packs/system/system.pkg").exists()


# ---- interruptions ---------------------------------------------------------------------------


def test_an_interrupted_send_is_cleaned_up_by_the_next(ftpd: dict, tmp_path: Path) -> None:
    sd = ftpd["root"]
    mod = make_mod(
        tmp_path / "a", {f"contents/010093801237c000/romfs/f{i}.bin": b"x" for i in range(5)}
    )
    recorded: list[str] = []
    calls = {"n": 0}

    def stop_after_two() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    with connect(ftpd) as ftp, pytest.raises(Cancelled):
        push(
            ftp, mod, seed_id="s", previous=[], options=PushOptions(),
            record_planned=recorded.extend, on_progress=lambda _p: None, cancelled=stop_after_two,
        )  # fmt: skip
    assert len(list((sd / DREAD / "romfs").iterdir())) == 2  # a partial install
    assert sd_manifest(sd)["state"] == "installing"
    assert len(recorded) == 5  # everything it planned to write was recorded first

    send(ftpd, make_mod(tmp_path / "b", {"contents/010093801237c000/romfs/new.bin": b"n"}),
         previous=recorded)  # fmt: skip
    assert sorted(p.name for p in (sd / DREAD / "romfs").iterdir()) == ["new.bin"]


# ---- through the API and the seed page -------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    romfs = make_romfs(tmp_path / "romfs")
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        runner=fake_runner,
        romfs=RomfsInspector(romfs, identify=fake_identify),
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


def patched_seed(client: TestClient) -> str:
    seed = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H).json()
    assert wait_for_job(client, seed["job_id"])["status"] == "succeeded"
    patch = client.post(f"/api/v1/seeds/{seed['seed_id']}/mod", headers=H).json()
    assert wait_for_job(client, patch["job_id"])["status"] == "succeeded"
    return seed["seed_id"]


def add_switch(client: TestClient, server: dict) -> str:
    body = {"name": "Living room", "host": server["host"], "port": server["port"]}
    return client.post("/api/v1/switches", json=body, headers=H).json()["id"]


def test_send_to_switch_end_to_end(client: TestClient, ftpd: dict) -> None:
    seed_id = patched_seed(client)
    switch_id = add_switch(client, ftpd)
    r = client.post(f"/api/v1/seeds/{seed_id}/push", json={"switch_id": switch_id}, headers=H)
    assert r.status_code == 202, r.text
    job = wait_for_job(client, r.json()["job_id"])
    assert (job["kind"], job["status"], job["error"]) == ("push", "succeeded", None)
    assert job["progress"].startswith("Sent.")
    assert (ftpd["root"] / DREAD / "romfs/packs/system/system.pkg").exists()

    switch = client.get(f"/api/v1/switches/{switch_id}").json()
    assert (switch["installed_seed_id"], switch["install_state"]) == (seed_id, "installed")
    assert client.get(f"/api/v1/seeds/{seed_id}").json()["push_job_id"] == r.json()["job_id"]
    assert "Sent to Living room" in client.get(f"/seeds/{seed_id}").text

    # Sending again replaces the seed cleanly (the server's manifest now has it).
    again = client.post(f"/api/v1/seeds/{seed_id}/push", json={"switch_id": switch_id}, headers=H)
    assert wait_for_job(client, again.json()["job_id"])["status"] == "succeeded"


def test_a_conflict_is_reported_on_the_job(client: TestClient, ftpd: dict) -> None:
    theirs = ftpd["root"] / DREAD / "romfs/packs/system/system.pkg"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"another mod")
    seed_id, switch_id = patched_seed(client), add_switch(client, ftpd)
    r = client.post(f"/api/v1/seeds/{seed_id}/push", json={"switch_id": switch_id}, headers=H)
    job = wait_for_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "belong to another mod" in job["error"]
    assert client.get(f"/api/v1/seeds/{seed_id}").json()["mod"]["status"] == "ready"  # unaffected
    body = {"switch_id": switch_id, "overwrite": True}
    r = client.post(f"/api/v1/seeds/{seed_id}/push", json=body, headers=H)
    assert wait_for_job(client, r.json()["job_id"])["status"] == "succeeded"


def test_ftpd_closed_fails_with_a_clear_message(client: TestClient) -> None:
    seed_id = patched_seed(client)
    switch_id = add_switch(client, {"host": "127.0.0.1", "port": 1})
    r = client.post(f"/api/v1/seeds/{seed_id}/push", json={"switch_id": switch_id}, headers=H)
    job = wait_for_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "refused the connection" in job["error"]


def test_what_can_be_sent(client: TestClient, ftpd: dict) -> None:
    switch_id = add_switch(client, ftpd)
    seed = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H).json()
    wait_for_job(client, seed["job_id"])
    r = client.post(
        f"/api/v1/seeds/{seed['seed_id']}/push", json={"switch_id": switch_id}, headers=H
    )
    assert (r.status_code, r.json()["detail"]) == (409, "patch this seed for Switch first")
    seed_id = patched_seed(client)
    missing = "00000000-0000-4000-8000-000000000000"
    r = client.post(f"/api/v1/seeds/{seed_id}/push", json={"switch_id": missing}, headers=H)
    assert r.status_code == 409
    assert "isn't saved" in r.json()["detail"]
    assert client.post(f"/api/v1/seeds/{missing}/push", json={"switch_id": switch_id},
                       headers=H).status_code == 404  # fmt: skip
    assert (
        client.post(f"/api/v1/seeds/{seed_id}/push", json={"switch_id": switch_id}).status_code
        == 403
    )


def test_the_seed_page_guides_sending(client: TestClient, ftpd: dict) -> None:
    seed_id = patched_seed(client)
    page = client.get(f"/seeds/{seed_id}").text
    assert "add your Switch" in page
    switch_id = add_switch(client, ftpd)
    page = client.get(f"/seeds/{seed_id}").text
    assert "Open <strong>ftpd</strong> on your Switch" in page
    assert f'value="{switch_id}"' in page
    r = client.post(f"/seeds/{seed_id}/push", data={"switch_id": switch_id},
                    headers={**H, "HX-Request": "true"})  # fmt: skip
    assert r.status_code == 204
    deadline = time.monotonic() + 10
    while "Sent to Living room" not in client.get(f"/seeds/{seed_id}/push/status").text:
        assert time.monotonic() < deadline
        time.sleep(0.05)
