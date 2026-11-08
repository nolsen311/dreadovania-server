# SPDX-License-Identifier: GPL-3.0-or-later
"""Game files status and validation (ADR 0013, Phase 2 PR 1).

No real game files are used anywhere: the RomFS here is synthetic, and version
detection is injected, keyed on the synthetic files.toc contents.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dreadovania import auth, romfs
from dreadovania.app import create_app
from dreadovania.config import Settings
from dreadovania.romfs import REQUIRED_FILES, RomfsInspector

ORIGIN = "https://dread.test"
H = {"Origin": ORIGIN}
FAKE_TOCS = {b"toc-1.0.0": "1.0.0", b"toc-2.0.0": "2.0.0", b"toc-2.1.0": "2.1.0"}


def fake_identify(toc: bytes) -> str | None:
    return FAKE_TOCS.get(toc)


def make_romfs(root: Path, toc: bytes = b"toc-2.1.0", skip: tuple[str, ...] = ()) -> Path:
    for name in REQUIRED_FILES:
        if name in skip:
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(toc if name == romfs.TOC else b"x" * 10)
    return root


def status(root: Path):
    return RomfsInspector(root, identify=fake_identify).status()


# ---- the inspector ---------------------------------------------------------------------------


def test_missing_and_empty_folders(tmp_path: Path) -> None:
    assert status(tmp_path / "nope").state == "missing"
    (tmp_path / "empty").mkdir()
    assert status(tmp_path / "empty").state == "missing"


def test_a_supported_romfs_is_ready(tmp_path: Path) -> None:
    s = status(make_romfs(tmp_path / "r"))
    assert (s.state, s.ready, s.version) == ("ready", True, "2.1.0")
    assert s.file_count == len(REQUIRED_FILES)
    assert s.total_bytes == 10 * (len(REQUIRED_FILES) - 1) + len(b"toc-2.1.0")
    assert status(make_romfs(tmp_path / "old", toc=b"toc-1.0.0")).version == "1.0.0"


def test_known_but_unsupported_and_unknown_versions(tmp_path: Path) -> None:
    s = status(make_romfs(tmp_path / "a", toc=b"toc-2.0.0"))
    assert (s.state, s.version, s.ready) == ("unsupported_version", "2.0.0", False)
    assert "2.1.0" in s.message
    s = status(make_romfs(tmp_path / "b", toc=b"something else"))
    assert (s.state, s.version) == ("unknown_version", None)


def test_incomplete_romfs_lists_the_missing_files(tmp_path: Path) -> None:
    s = status(make_romfs(tmp_path / "r", skip=("packs/system/system.pkg",)))
    assert s.state == "not_romfs"
    assert "packs/system/system.pkg" in s.message


def test_a_romfs_one_folder_too_deep_is_explained(tmp_path: Path) -> None:
    make_romfs(tmp_path / "r" / "romfs")
    assert status(tmp_path / "r").state == "nested"


def test_game_packages_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / "Game.XCI").write_bytes(b"\0")
    s = status(root)
    assert s.state == "package"
    assert "keys" not in s.message.lower()


def test_an_already_patched_romfs_is_refused(tmp_path: Path) -> None:
    root = make_romfs(tmp_path / "r")
    (root / romfs.PATCHED_MARKER).write_text("{}")
    assert status(root).state == "modified"


def test_symlinks_are_not_followed_when_counting(tmp_path: Path) -> None:
    root = make_romfs(tmp_path / "r")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"y" * 1000)
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "filelink").symlink_to(outside / "big")
    assert status(root).file_count == len(REQUIRED_FILES)


def test_an_unreadable_folder_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_romfs(tmp_path / "r")

    def denied(self: Path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "iterdir", denied)
    s = status(root)
    assert s.state == "unreadable"
    assert "10001" in s.message
    assert str(root) not in s.message


def test_messages_never_reveal_the_folder_path(tmp_path: Path) -> None:
    secret = tmp_path / "my-private-library"
    cases = [secret, make_romfs(secret / "a", skip=(romfs.TOC,)), make_romfs(secret / "b")]
    for root in cases:
        assert "my-private-library" not in status(root).message


# ---- the library's version table ----------------------------------------------------------------


def test_the_library_table_has_the_supported_versions() -> None:
    # If an update to mercury-engine-data-structures drops or renames these,
    # patching would silently stop working: fail here instead.
    versions = set(romfs.dread_versions_by_toc_md5().values())
    assert set(romfs.SUPPORTED_VERSIONS) <= versions


def test_real_identification_uses_the_toc_md5(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    toc = b"pretend toc"
    digest = hashlib.md5(toc, usedforsecurity=False).digest()
    monkeypatch.setattr(romfs, "dread_versions_by_toc_md5", lambda: {digest: "2.1.0"})
    assert romfs.identify_by_toc(toc) == "2.1.0"
    assert romfs.identify_by_toc(b"other") is None


def test_settings_default_romfs_folder(tmp_path: Path) -> None:
    assert Settings(data_dir=tmp_path).romfs_path == tmp_path / "romfs"
    assert Settings(data_dir=tmp_path, romfs_dir=tmp_path / "x").romfs_path == tmp_path / "x"


# ---- API and UI -------------------------------------------------------------------------------


@pytest.fixture
def romfs_dir(tmp_path: Path) -> Path:
    return tmp_path / "romfs"


@pytest.fixture
def client(tmp_path: Path, romfs_dir: Path, fake_runner) -> Iterator[TestClient]:
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


def test_api_reports_status_and_follows_changes(client: TestClient, romfs_dir: Path) -> None:
    body = client.get("/api/v1/romfs").json()
    assert body["state"] == "missing"
    assert body["ready"] is False
    assert body["supported_versions"] == ["1.0.0", "2.1.0"]
    make_romfs(romfs_dir)
    body = client.get("/api/v1/romfs").json()
    assert (body["state"], body["ready"], body["version"]) == ("ready", True, "2.1.0")
    assert body["file_count"] == len(REQUIRED_FILES)
    assert set(body) == {
        "state", "ready", "message", "version", "supported_versions", "file_count", "total_bytes",
        "upload_allowed", "upload_blocked_reason",
    }  # fmt: skip
    assert (body["upload_allowed"], body["upload_blocked_reason"]) == (True, None)


def test_api_needs_a_session(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/romfs").status_code == 401


def test_game_files_page(client: TestClient, romfs_dir: Path) -> None:
    page = client.get("/game-files")
    assert page.status_code == 200
    assert "not set up" in page.text
    assert "/data/romfs" in page.text
    make_romfs(romfs_dir, toc=b"toc-1.0.0")
    page = client.get("/game-files")
    assert "Ready: Dread 1.0.0." in page.text
    assert str(romfs_dir) not in page.text
    assert 'href="/game-files"' in client.get("/").text


def test_default_inspector_uses_the_configured_folder(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", romfs_dir=tmp_path / "games"))
    with TestClient(app, base_url=ORIGIN):
        assert app.state.romfs.root == tmp_path / "games"
