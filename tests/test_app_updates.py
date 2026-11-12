# SPDX-License-Identifier: GPL-3.0-or-later
"""Offering the Switch app's latest verified release (ADR 0021).

A fake GitHub stands in for the network. The signature check itself is tested
against the real build-provenance bundle of dreadovania-nx v0.4.0 (tests/data),
with Sigstore's built-in trust root, so it needs no network either.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import H
from test_devices import as_device, pair, pairing_code
from test_passkeys import make_client

from dreadovania import app_updates
from dreadovania.app import create_app
from dreadovania.app_updates import AppUpdates, UpdateProblem, verify_attestation
from dreadovania.config import Settings

DATA = Path(__file__).parent / "data"
V040_SHA256 = "db7cbb625fa44719ff14b5e89df4a81b54f2d81190a92c5897a799bfe60f7edb"
API = "https://api.github.com/repos/nolsen311/dreadovania-nx"
NRO = b"NRO0 fake app" * 100


class FakeGitHub:
    def __init__(self, tag: str = "v0.4.2", nro: bytes = NRO, checksum: str | None = None):
        self.tag, self.nro = tag, nro
        self.checksum = checksum or hashlib.sha256(nro).hexdigest()
        self.calls: list[str] = []
        self.attestations = [{"bundle": {"fake": True}}]

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        dl = f"https://github.com/nolsen311/dreadovania-nx/releases/download/{self.tag}"
        if url == f"{API}/releases/latest":
            return json.dumps({
                "tag_name": self.tag,
                "assets": [
                    {"name": "dreadovania.nro", "browser_download_url": f"{dl}/dreadovania.nro"},
                    {"name": "dreadovania.nro.sha256",
                     "browser_download_url": f"{dl}/dreadovania.nro.sha256"},
                ],
            }).encode()  # fmt: skip
        if url == f"{dl}/dreadovania.nro":
            return self.nro
        if url == f"{dl}/dreadovania.nro.sha256":
            return f"{self.checksum}  dreadovania.nro\n".encode()
        if url.startswith(f"{API}/attestations/sha256:"):
            return json.dumps({"attestations": self.attestations}).encode()
        raise OSError(f"unexpected URL {url}")


def accept(bundle: str, sha256: str, tag: str) -> None:
    pass


def refuse(bundle: str, sha256: str, tag: str) -> None:
    raise UpdateProblem("the release's signature didn't verify: test")


# ---- the real signature check -------------------------------------------------------------------


def test_the_real_v040_release_verifies() -> None:
    bundle = (DATA / "nx-v0.4.0-attestation.json").read_text()
    verify_attestation(bundle, V040_SHA256, "v0.4.0")


def test_a_signature_for_another_tag_or_file_is_refused() -> None:
    bundle = (DATA / "nx-v0.4.0-attestation.json").read_text()
    with pytest.raises(UpdateProblem, match="didn't verify"):
        verify_attestation(bundle, V040_SHA256, "v9.9.9")  # signed for v0.4.0, not v9.9.9
    with pytest.raises(UpdateProblem, match="different file"):
        verify_attestation(bundle, "0" * 64, "v0.4.0")
    with pytest.raises(UpdateProblem, match="didn't verify"):
        verify_attestation(bundle, V040_SHA256, "v0.4.0", repo="someone/else")
    with pytest.raises(UpdateProblem):
        verify_attestation("{}", V040_SHA256, "v0.4.0")


def _image_xdg_dirs(root: Path) -> dict[str, str]:
    """The Dockerfile's XDG_* settings, with /tmp moved under ``root``."""
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    found = dict(re.findall(r"(XDG_[A-Z_]+)=(\S+)", dockerfile))
    assert set(found) == {"XDG_CACHE_HOME", "XDG_DATA_HOME"}
    tmp = "/tmp/"  # noqa: S108 (the image's tmpfs, moved under root here)
    return {k: str(root / v.removeprefix(tmp)) for k, v in found.items()}


def _verify_in_a_fresh_process(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    code = (
        "from pathlib import Path; from dreadovania.app_updates import verify_attestation;"
        f"verify_attestation(Path({str(DATA / 'nx-v0.4.0-attestation.json')!r}).read_text(),"
        f" {V040_SHA256!r}, 'v0.4.0')"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    # The image's user has no usable home directory; /dev/null can't hold one even for root.
    env |= {"HOME": "/dev/null", **env_extra}
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )


def test_the_check_works_without_a_home_directory(tmp_path: Path) -> None:
    """As in the container: HOME is unusable, and the Dockerfile's XDG_* dirs are used."""
    without = _verify_in_a_fresh_process({})
    assert without.returncode != 0 and "/dev/null" in without.stderr  # the bug this guards
    done = _verify_in_a_fresh_process(_image_xdg_dirs(tmp_path))
    assert done.returncode == 0, done.stderr


# ---- finding, checking and caching a release ----------------------------------------------------


def test_a_verified_release_is_downloaded_once_and_kept(tmp_path: Path) -> None:
    github = FakeGitHub()
    clock = [0.0]
    updates = AppUpdates(tmp_path, fetch=github, verify=accept, clock=lambda: clock[0])
    release = updates.latest()
    assert (release.version, release.size) == ("0.4.2", len(NRO))
    assert release.sha256 == hashlib.sha256(NRO).hexdigest()
    assert release.path.read_bytes() == NRO
    calls = len(github.calls)
    updates.latest()
    assert len(github.calls) == calls  # cached
    clock[0] += app_updates.CHECK_EVERY
    again = AppUpdates(tmp_path, fetch=github, verify=refuse)  # after a restart
    assert again.latest().version == "0.4.2"  # kept on disk, not re-verified
    assert not any(c.endswith(".nro") for c in github.calls[calls + 1 :])


@pytest.mark.parametrize(
    ("github", "verify", "reason"),
    [
        (FakeGitHub(checksum="0" * 64), accept, "doesn't match its checksum"),
        (FakeGitHub(), refuse, "didn't verify"),
        (FakeGitHub(tag="nightly"), accept, "isn't a version number"),
    ],
)
def test_nothing_unverified_is_offered(tmp_path: Path, github, verify, reason: str) -> None:
    updates = AppUpdates(tmp_path, fetch=github, verify=verify)
    with pytest.raises(UpdateProblem, match=reason):
        updates.latest()
    assert not list(tmp_path.rglob("*.nro"))


def test_only_github_release_downloads_are_fetched(tmp_path: Path) -> None:
    elsewhere = "http://example.com/dreadovania.nro"

    class Elsewhere(FakeGitHub):
        def __call__(self, url: str) -> bytes:
            data = super().__call__(url)
            if url.endswith("/releases/latest"):
                release = json.loads(data)
                release["assets"][0]["browser_download_url"] = elsewhere
                return json.dumps(release).encode()
            return data

    github = Elsewhere()
    with pytest.raises(UpdateProblem, match="aren't GitHub release downloads"):
        AppUpdates(tmp_path, fetch=github, verify=accept).latest()
    assert elsewhere not in github.calls


def test_no_attestation_means_no_update(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.attestations = []
    with pytest.raises(UpdateProblem, match="no attestation"):
        AppUpdates(tmp_path, fetch=github, verify=accept).latest()


def test_offline_and_turned_off(tmp_path: Path) -> None:
    def offline(url: str) -> bytes:
        raise OSError("no network")

    with pytest.raises(UpdateProblem, match="couldn't reach GitHub"):
        AppUpdates(tmp_path, fetch=offline).latest()
    github = FakeGitHub()
    with pytest.raises(UpdateProblem, match="turned off"):
        AppUpdates(tmp_path, enabled=False, fetch=github).latest()
    assert github.calls == []  # never contacts GitHub


def test_an_older_version_is_removed(tmp_path: Path) -> None:
    AppUpdates(tmp_path, fetch=FakeGitHub(tag="v0.4.1"), verify=accept).latest()
    AppUpdates(tmp_path, fetch=FakeGitHub(tag="v0.4.2"), verify=accept).latest()
    assert sorted(p.name for p in (tmp_path / "app").iterdir()) == ["0.4.2"]


# ---- the routes ---------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    c = make_client(tmp_path, fake_runner)
    c.app.state.app_updates = AppUpdates(tmp_path / "data", fetch=FakeGitHub(), verify=accept)
    yield c
    c.__exit__(None, None, None)


def test_a_paired_switch_can_check_and_download(client: TestClient) -> None:
    device = pair(client, pairing_code(client))
    client.cookies.clear()
    headers = as_device(device["token"])
    latest = client.get("/api/v1/app/latest", headers=headers)
    assert latest.status_code == 200, latest.text
    body = latest.json()
    assert body["available"] is True and body["version"] == "0.4.2"
    assert body["minimum_version"] == app_updates.MINIMUM_APP_VERSION
    nro = client.get("/api/v1/app/dreadovania.nro", headers=headers)
    assert nro.status_code == 200
    assert nro.content == NRO
    assert hashlib.sha256(nro.content).hexdigest() == body["sha256"]
    assert nro.headers["x-content-sha256"] == body["sha256"]


def test_no_release_is_explained(client: TestClient) -> None:
    client.app.state.app_updates = AppUpdates(Path("/nonexistent"), enabled=False)
    body = client.get("/api/v1/app/latest", headers=H).json()
    assert body == {"available": False, "version": None, "size": None, "sha256": None,
                    "minimum_version": app_updates.MINIMUM_APP_VERSION,
                    "reason": "app updates are turned off on this server"}  # fmt: skip
    r = client.get("/api/v1/app/dreadovania.nro", headers=H)
    assert r.status_code == 404


def test_signed_in_only(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/app/latest").status_code == 401


def test_the_setting_turns_it_off(tmp_path: Path, fake_runner) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", app_updates=False), runner=fake_runner)
    with TestClient(app):
        assert app.state.app_updates.enabled is False


def test_versions_parse() -> None:
    assert app_updates.parse_version("v0.4.2") == (0, 4, 2)
    assert app_updates.parse_version("0.10.0") == (0, 10, 0)
    assert app_updates.parse_version("v1.0") is None
    assert app_updates.parse_version("latest") is None


def test_checking_now_asks_github_again_at_most_every_few_minutes(tmp_path: Path) -> None:
    github = FakeGitHub(tag="v0.4.1")
    clock = [0.0]
    updates = AppUpdates(tmp_path, fetch=github, verify=accept, clock=lambda: clock[0])
    assert updates.latest().version == "0.4.1"
    github.tag = "v0.4.2"  # a new release appears
    clock[0] += 60
    assert updates.latest().version == "0.4.1"  # the last answer, without check_now
    assert updates.latest(check_now=True).version == "0.4.1"  # asked a minute ago: too soon
    clock[0] += app_updates.CHECK_NOW_EVERY
    calls = len(github.calls)
    assert updates.latest(check_now=True).version == "0.4.2"  # asked again
    assert len(github.calls) > calls
    calls = len(github.calls)
    assert updates.latest(check_now=True).version == "0.4.2"
    assert len(github.calls) == calls  # and not again right away


def test_the_app_can_ask_to_check_now(client: TestClient) -> None:
    github = FakeGitHub(tag="v0.4.1")
    clock = [0.0]
    client.app.state.app_updates = AppUpdates(
        Path(client.app.state.settings.data_dir), fetch=github, verify=accept,
        clock=lambda: clock[0],
    )  # fmt: skip
    assert client.get("/api/v1/app/latest", headers=H).json()["version"] == "0.4.1"
    github.tag = "v0.4.2"
    clock[0] += app_updates.CHECK_NOW_EVERY
    assert client.get("/api/v1/app/latest", headers=H).json()["version"] == "0.4.1"
    latest = client.get("/api/v1/app/latest?check=true", headers=H).json()
    assert latest["version"] == "0.4.2"
