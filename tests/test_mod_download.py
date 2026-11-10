# SPDX-License-Identifier: GPL-3.0-or-later
"""The Switch app's download of a patched seed: a manifest and a tar (ADR 0018)."""

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from test_api import H
from test_push import client, patched_seed  # noqa: F401 (the client fixture)

from dreadovania.mods import ModError, iter_mod_tar, mod_manifest, tar_size


def files_in(tar_bytes: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        assert all(m.isfile() for m in archive.getmembers())
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers()}


def test_the_manifest_matches_the_tar(client) -> None:  # noqa: F811
    seed_id = patched_seed(client)
    manifest = client.get(f"/api/v1/seeds/{seed_id}/mod/manifest").json()
    assert manifest["file_count"] == 3
    assert manifest["size_bytes"] == 1013
    r = client.get(f"/api/v1/seeds/{seed_id}/mod.tar")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-tar"
    assert int(r.headers["content-length"]) == len(r.content) == manifest["tar_bytes"]
    assert len(r.content) % 512 == 0
    files = files_in(r.content)
    assert [f["path"] for f in manifest["files"]] == list(files)  # same order
    for f in manifest["files"]:
        assert f["path"].startswith("atmosphere/")
        assert len(files[f["path"]]) == f["size"]
        assert hashlib.sha256(files[f["path"]]).hexdigest() == f["sha256"]
    assert "atmosphere/patcher.json" not in files


def test_the_paired_switch_can_download(client) -> None:  # noqa: F811
    seed_id = patched_seed(client)
    code = client.post("/api/v1/devices/pairing-code", headers=H).json()["code"]
    token = client.post("/api/v1/devices/pair", json={"code": code, "name": "S"}).json()["token"]
    client.cookies.clear()
    device = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/v1/seeds/{seed_id}/mod/manifest", headers=device).status_code == 200
    assert client.get(f"/api/v1/seeds/{seed_id}/mod.tar", headers=device).status_code == 200
    assert client.get(f"/api/v1/seeds/{seed_id}/mod.zip", headers=device).status_code == 403


def test_unpatched_and_unknown_seeds(client) -> None:  # noqa: F811
    seed = client.post("/api/v1/seeds", json={"preset_id": "starter_preset"}, headers=H).json()
    from test_api import wait_for_job

    wait_for_job(client, seed["job_id"])
    r = client.get(f"/api/v1/seeds/{seed['seed_id']}/mod/manifest")
    assert (r.status_code, r.json()["detail"]) == (409, "patch this seed first")
    assert client.get(f"/api/v1/seeds/{seed['seed_id']}/mod.tar").status_code == 409
    missing = "00000000-0000-4000-8000-000000000000"
    assert client.get(f"/api/v1/seeds/{missing}/mod/manifest").status_code == 404


def test_long_names_use_the_ustar_prefix(tmp_path: Path) -> None:
    root = tmp_path / "mod"
    deep = "contents/010093801237c000/romfs/actors/characters/chozocommander/charclasses"
    (root / deep).mkdir(parents=True)
    (root / deep / "chozocommander.bmsad").write_bytes(b"x" * 700)
    data = b"".join(iter_mod_tar(root))
    assert len(data) == tar_size(root)
    name = f"atmosphere/{deep}/chozocommander.bmsad"
    assert len(name) > 100  # beyond ustar's name field, so the prefix is used
    assert files_in(data) == {name: b"x" * 700}
    header = data[:512]
    assert header[257:263] == b"ustar\x00"  # plain ustar, no PAX records
    assert mod_manifest(root)[0]["path"] == name


def test_names_too_long_for_ustar_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "mod"
    long_dir = "contents/" + "/".join(["x" * 60] * 4)
    (root / long_dir).mkdir(parents=True)
    (root / long_dir / ("y" * 120)).write_bytes(b"1")
    with pytest.raises(ModError, match="too long"):
        b"".join(iter_mod_tar(root))
