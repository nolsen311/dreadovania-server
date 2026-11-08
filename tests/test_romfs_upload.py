# SPDX-License-Identifier: GPL-3.0-or-later
"""Uploading a RomFS zip (ADR 0013, Phase 2 PR 3).

Every zip here is synthetic: a few small files laid out like a RomFS, with
version detection injected. No game data is used.
"""

import io
import json
import os
import stat
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import ORIGIN, H
from test_romfs import fake_identify

from dreadovania import auth
from dreadovania.app import create_app
from dreadovania.config import Settings
from dreadovania.romfs import REQUIRED_FILES, TOC, RomfsInspector
from dreadovania.romfs_upload import (
    OffsetMismatch,
    RomfsUploads,
    UploadError,
    UploadNotFound,
    extract,
    plan_extraction,
)

pytestmark = pytest.mark.anyio
CHUNK = 64 * 1024


def romfs_zip(
    toc: bytes = b"toc-2.1.0",
    prefix: str = "",
    extra: dict[str, bytes] | None = None,
    filler: int = 0,
) -> bytes:
    """A zip of a synthetic RomFS. ``filler`` adds a file of that many bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in REQUIRED_FILES:
            archive.writestr(prefix + name, toc if name == TOC else b"x" * 10)
        if filler:
            archive.writestr(prefix + "packs/filler.bin", os.urandom(filler))
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


async def chunks(data: bytes, size: int = 8192) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def uploads(data_dir: Path) -> RomfsUploads:
    return RomfsUploads(
        data_dir,
        RomfsInspector(data_dir / "romfs", identify=fake_identify),
        max_bytes=10 * 1024**2,
        max_files=100,
        chunk_size=CHUNK,
    )


async def upload_all(uploads: RomfsUploads, data: bytes, fingerprint: str = "f") -> str:
    status, created = await uploads.start(len(data), fingerprint)
    assert created
    while status.offset < len(data):
        piece = data[status.offset : status.offset + CHUNK]
        status = await uploads.write(status.id, status.offset, chunks(piece))
    assert status.state == "received"
    return status.id


async def install(uploads: RomfsUploads, upload_id: str):
    await uploads.install(upload_id)
    await uploads.wait_for_install()
    return uploads.current()


# ---- the whole flow --------------------------------------------------------------------------


async def test_upload_and_install(uploads: RomfsUploads, data_dir: Path) -> None:
    data = romfs_zip(filler=200_000)
    upload_id = await upload_all(uploads, data)
    result = await install(uploads, upload_id)
    assert (result.state, result.message) == ("installed", "Installed: Dread 2.1.0.")
    status = uploads.romfs.status()
    assert (status.ready, status.version) == (True, "2.1.0")
    assert not (uploads.dir / "romfs.zip").exists()  # the zip is removed afterwards
    assert not (uploads.dir / "staging").exists()


async def test_a_new_upload_replaces_the_old_game_files(uploads: RomfsUploads, data_dir) -> None:
    await install(uploads, await upload_all(uploads, romfs_zip(toc=b"toc-1.0.0"), "a"))
    assert uploads.romfs.status().version == "1.0.0"
    (data_dir / "romfs" / "leftover.txt").write_text("from the old dump")
    await install(uploads, await upload_all(uploads, romfs_zip(toc=b"toc-2.1.0"), "b"))
    assert uploads.romfs.status().version == "2.1.0"
    assert not (data_dir / "romfs" / "leftover.txt").exists()
    assert sorted(p.name for p in data_dir.iterdir()) == ["romfs", "uploads"]  # no old copies


async def test_a_romfs_inside_one_folder_is_found(uploads: RomfsUploads, data_dir) -> None:
    data = romfs_zip(prefix="MyDump/romfs/", extra={"__MACOSX/._x": b"junk", "readme.txt": b"hi"})
    result = await install(uploads, await upload_all(uploads, data))
    assert result.state == "installed"
    assert (data_dir / "romfs" / TOC).is_file()
    assert not (data_dir / "romfs" / "readme.txt").exists()  # outside the RomFS: ignored


async def test_an_invalid_romfs_never_replaces_the_current_one(uploads, data_dir) -> None:
    await install(uploads, await upload_all(uploads, romfs_zip(toc=b"toc-2.1.0"), "good"))
    result = await install(uploads, await upload_all(uploads, romfs_zip(toc=b"toc-2.0.0"), "old"))
    assert result.state == "failed"
    assert "2.0.0" in result.message
    assert uploads.romfs.status().version == "2.1.0"  # still the previous game files


async def test_a_file_that_isnt_a_zip_fails_cleanly(uploads: RomfsUploads) -> None:
    result = await install(uploads, await upload_all(uploads, b"not a zip at all" * 100))
    assert result.state == "failed"
    assert "isn't a valid zip" in result.message
    assert not (uploads.romfs.root).exists()


# ---- resuming and the chunk protocol ---------------------------------------------------------


async def test_the_same_file_resumes_where_it_stopped(uploads: RomfsUploads) -> None:
    data = romfs_zip(filler=150_000)
    status, created = await uploads.start(len(data), "name|size|mtime")
    assert created
    status = await uploads.write(status.id, 0, chunks(data[:CHUNK]))
    again, created = await uploads.start(len(data), "name|size|mtime")
    assert not created
    assert (again.id, again.offset, again.state) == (status.id, CHUNK, "receiving")

    other, created = await uploads.start(len(data), "another file")
    assert created
    assert other.id != status.id
    assert other.offset == 0


async def test_chunks_must_start_at_the_current_offset(uploads: RomfsUploads) -> None:
    data = romfs_zip(filler=150_000)
    status, _ = await uploads.start(len(data), "f")
    await uploads.write(status.id, 0, chunks(data[:1000]))
    with pytest.raises(OffsetMismatch) as raised:
        await uploads.write(status.id, 0, chunks(data[:1000]))
    assert raised.value.expected == 1000


async def test_chunks_are_limited_in_size(uploads: RomfsUploads) -> None:
    data = romfs_zip(filler=150_000)
    status, _ = await uploads.start(len(data), "f")
    with pytest.raises(UploadError, match="larger than allowed"):
        await uploads.write(status.id, 0, chunks(data[: CHUNK + 1]))
    # ...and never past the declared size.
    small, _ = await uploads.start(10, "tiny")
    with pytest.raises(UploadError, match="larger than allowed"):
        await uploads.write(small.id, 0, chunks(b"x" * 11))


async def test_size_limits_and_unknown_ids(uploads: RomfsUploads) -> None:
    with pytest.raises(UploadError, match="larger than this server accepts"):
        await uploads.start(uploads.max_bytes + 1, "f")
    with pytest.raises(UploadError, match="empty"):
        await uploads.start(0, "f")
    status, _ = await uploads.start(100, "f")
    with pytest.raises(UploadNotFound):
        await uploads.write("0" * 32, 0, chunks(b"x"))
    with pytest.raises(UploadError, match="isn't complete"):
        await uploads.install(status.id)


async def test_discarding_an_upload(uploads: RomfsUploads) -> None:
    status, _ = await uploads.start(100, "f")
    await uploads.write(status.id, 0, chunks(b"x" * 50))
    await uploads.discard(status.id)
    assert uploads.current() is None
    assert not uploads.dir.exists()


async def test_no_install_while_a_seed_is_being_patched(uploads: RomfsUploads) -> None:
    upload_id = await upload_all(uploads, romfs_zip())
    uploads.is_busy = lambda: True
    with pytest.raises(UploadError, match="being patched"):
        await uploads.install(upload_id)


async def test_uploads_are_refused_when_the_folder_is_mounted(uploads, monkeypatch) -> None:
    monkeypatch.setattr(os.path, "ismount", lambda p: Path(p) == uploads.romfs.root)
    assert "mounted" in uploads.unavailable_reason()
    with pytest.raises(UploadError, match="mounted"):
        await uploads.start(100, "f")


def test_recovery_after_a_restart(uploads: RomfsUploads) -> None:
    uploads.dir.mkdir(parents=True)
    (uploads.dir / "romfs.zip").write_bytes(b"x" * 10)
    (uploads.dir / "staging").mkdir()
    meta = {"id": "a" * 32, "size": 10, "fingerprint": "h", "state": "installing",
            "message": None, "updated_at": time.time()}  # fmt: skip
    (uploads.dir / "upload.json").write_text(json.dumps(meta))
    uploads.recover()
    current = uploads.current()
    assert (current.state, current.offset) == ("received", 10)
    assert "restarted" in current.message
    assert not (uploads.dir / "staging").exists()

    meta["updated_at"] = time.time() - 30 * 24 * 3600  # abandoned a month ago
    (uploads.dir / "upload.json").write_text(json.dumps(meta))
    uploads.recover()
    assert uploads.current() is None


# ---- what a zip may contain ------------------------------------------------------------------


def plan(data: bytes, max_files: int = 100, max_bytes: int = 10**7):
    return plan_extraction(zipfile.ZipFile(io.BytesIO(data)), max_files, max_bytes)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"../escape": b"x"}, "unsafe path"),
        ({"packs//double": b"x"}, "unsafe path"),
        ({"C:/windows": b"x"}, "unsafe path"),
        ({"packs/./dot": b"x"}, "unsafe path"),
    ],
)
def test_unsafe_paths_are_refused(extra, message) -> None:
    with pytest.raises(UploadError, match=message):
        plan(romfs_zip(extra=extra))


def test_backslash_paths_are_normalised_and_checked() -> None:
    entries, _ = plan(romfs_zip(extra={"packs\\sub\\file.bin": b"x"}))
    assert ("packs", "sub", "file.bin") in [parts for _info, parts in entries]
    with pytest.raises(UploadError, match="unsafe path"):
        plan(romfs_zip(extra={"..\\escape": b"x"}))


def test_symlinks_and_encryption_are_refused() -> None:
    buffer = io.BytesIO(romfs_zip())
    with zipfile.ZipFile(buffer, "a") as archive:
        link = zipfile.ZipInfo("packs/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    with pytest.raises(UploadError, match="symbolic link"):
        plan(buffer.getvalue())

    archive = zipfile.ZipFile(io.BytesIO(romfs_zip()))
    archive.infolist()[0].flag_bits |= 0x1
    with pytest.raises(UploadError, match="encrypted"):
        plan_extraction(archive, 100, 10**7)


def test_unsupported_compression_is_explained() -> None:
    archive = zipfile.ZipFile(io.BytesIO(romfs_zip()))
    archive.infolist()[0].compress_type = 9  # Deflate64, as Windows writes for large files
    with pytest.raises(UploadError, match="Deflate64"):
        plan_extraction(archive, 100, 10**7)


def test_packages_missing_and_multiple_romfs_are_explained() -> None:
    nsp = io.BytesIO()
    with zipfile.ZipFile(nsp, "w") as archive:
        archive.writestr("Game.nsp", b"\0")
    with pytest.raises(UploadError, match="game package"):
        plan(nsp.getvalue())
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("notes.txt", b"hi")
    with pytest.raises(UploadError, match=r"no system/files\.toc"):
        plan(empty.getvalue())
    with pytest.raises(UploadError, match="more than one RomFS"):
        plan(romfs_zip(extra={"copy/" + TOC: b"t"}))


def test_file_count_and_size_limits() -> None:
    with pytest.raises(UploadError, match="too many files"):
        plan(romfs_zip(), max_files=len(REQUIRED_FILES) - 1)
    with pytest.raises(UploadError, match="larger than this server accepts"):
        plan(romfs_zip(filler=5000), max_bytes=1000)


def test_duplicate_entries_are_refused() -> None:
    buffer = io.BytesIO(romfs_zip())
    with zipfile.ZipFile(buffer, "a") as archive, pytest.warns(UserWarning):
        archive.writestr("config.ini", b"again")
    with pytest.raises(UploadError, match="same file twice"):
        plan(buffer.getvalue())


def test_extraction_counts_real_bytes(tmp_path: Path) -> None:
    archive = zipfile.ZipFile(io.BytesIO(romfs_zip(filler=5000)))
    entries, _ = plan_extraction(archive, 100, 10**7)
    for info, _parts in entries:
        if info.filename.endswith("filler.bin"):
            info.file_size = 100  # the zip claims far less than it holds
    with pytest.raises((UploadError, zipfile.BadZipFile)):
        extract(archive, entries, tmp_path / "out", 10**7, lambda _done: None)


# ---- API and UI ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    data = tmp_path / "data"
    app = create_app(
        Settings(data_dir=data, romfs_upload_chunk_bytes=CHUNK),
        runner=fake_runner,
        romfs=RomfsInspector(data / "romfs", identify=fake_identify),
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


def test_api_upload_resume_and_install(client: TestClient) -> None:
    data = romfs_zip(filler=150_000)
    r = client.post("/api/v1/romfs/upload", json={"size": len(data), "fingerprint": "f"}, headers=H)
    assert r.status_code == 201, r.text
    upload = r.json()
    assert (upload["offset"], upload["chunk_size"], upload["state"]) == (0, CHUNK, "receiving")
    url = f"/api/v1/romfs/upload/{upload['id']}"

    patch = {**H, "Content-Type": "application/offset+octet-stream"}
    r = client.patch(url, content=data[:CHUNK], headers={**patch, "Upload-Offset": "0"})
    assert r.json()["offset"] == CHUNK
    # A repeated chunk is refused with the offset to continue from.
    r = client.patch(url, content=data[:CHUNK], headers={**patch, "Upload-Offset": "0"})
    assert r.status_code == 409
    assert (r.json()["offset"], r.headers["upload-offset"]) == (CHUNK, str(CHUNK))
    assert client.patch(url, content=b"x", headers=patch).status_code == 400  # no offset

    # Resuming the same file picks up where it stopped.
    r = client.post("/api/v1/romfs/upload", json={"size": len(data), "fingerprint": "f"}, headers=H)
    assert (r.status_code, r.json()["offset"]) == (200, CHUNK)

    offset = CHUNK
    while offset < len(data):
        r = client.patch(
            url,
            content=data[offset : offset + CHUNK],
            headers={**patch, "Upload-Offset": str(offset)},
        )
        offset = r.json()["offset"]
    assert r.json()["state"] == "received"

    assert client.post(f"{url}/install", headers=H).status_code == 202
    deadline = time.monotonic() + 10
    while (state := client.get("/api/v1/romfs/upload").json()["state"]) == "installing":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert state == "installed"
    romfs = client.get("/api/v1/romfs").json()
    assert (romfs["ready"], romfs["version"], romfs["upload_allowed"]) == (True, "2.1.0", True)


def test_api_upload_errors(client: TestClient) -> None:
    assert client.get("/api/v1/romfs/upload").status_code == 404
    r = client.post("/api/v1/romfs/upload", json={"size": 10**12, "fingerprint": "f"}, headers=H)
    assert r.status_code == 409
    assert "larger than this server accepts" in r.json()["detail"]
    upload = client.post("/api/v1/romfs/upload", json={"size": 10, "fingerprint": "f"}, headers=H)
    url = f"/api/v1/romfs/upload/{upload.json()['id']}"
    assert client.post(f"{url}/install", headers=H).status_code == 409
    assert (
        client.patch("/api/v1/romfs/upload/nope", headers={**H, "Upload-Offset": "0"}).status_code
        == 404
    )
    assert client.delete(url, headers=H).status_code == 204
    assert client.get("/api/v1/romfs/upload").status_code == 404


def test_api_upload_needs_sign_in_and_origin(client: TestClient) -> None:
    body = {"size": 10, "fingerprint": "f"}
    assert client.post("/api/v1/romfs/upload", json=body).status_code == 403  # CSRF
    client.cookies.clear()
    assert client.post("/api/v1/romfs/upload", json=body, headers=H).status_code == 401


def test_game_files_page_offers_upload(client: TestClient, monkeypatch) -> None:
    page = client.get("/game-files").text
    assert 'id="romfs-file"' in page
    upload = client.post("/api/v1/romfs/upload", json={"size": 100, "fingerprint": "f"}, headers=H)
    client.patch(
        f"/api/v1/romfs/upload/{upload.json()['id']}",
        content=b"x" * 40,
        headers={**H, "Upload-Offset": "0"},
    )
    page = client.get("/game-files").text
    assert "A previous upload stopped at 40 Bytes of 100 Bytes" in page
    assert 'id="romfs-discard" class="danger small">' in page

    monkeypatch.setattr(os.path, "ismount", lambda p: str(p).endswith("romfs"))
    page = client.get("/game-files").text
    assert "mounted from the host" in page
    assert 'id="romfs-file"' not in page
