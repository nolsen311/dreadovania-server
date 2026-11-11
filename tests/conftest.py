# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import contextlib
import json
import socket
import threading
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import ThreadedFTPServer

from dreadovania.db import Database, utc_now
from dreadovania.jobs import JobQueue
from dreadovania.runner import (
    ExportRequest,
    ExportResult,
    GenerateRequest,
    GenerateResult,
    ProgressCallback,
    RunnerError,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "data" / "dreadovania.db")
    database.migrate()
    return database


def make_user(db: Database, user_id: str, role: str = "admin") -> str:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO users (id, created_at, role) VALUES (?, ?, ?)", (user_id, utc_now(), role)
        )
    return user_id


def fake_rdvgame(race: bool) -> dict:
    """Shaped like Randovania's output: race seeds carry no spoiler (see ADR 0010)."""
    if race:
        return {"schema_version": 1, "checksum": None, "info": {"has_spoiler": False}}
    return {
        "schema_version": 1,
        "checksum": None,
        "info": {"has_spoiler": True},
        "item_order": [],
        "game_modifications": [
            {
                # Shapes copied from real Randovania 12.0.0 output.
                "starting_location": "Artaria/Intro Room/Start Point",
                "starting_equipment": {"pickups": ["Missiles", "Pulse Radar"]},
                "locations": [
                    {
                        "node_identifier": {
                            "region": "Artaria", "area": "Arbitrary Enky Room",
                            "node": "Pickup (Missile Tank)",
                        },
                        "index": 1,
                        "pickup": "Power Bomb Tank",
                    }
                ],
            }
        ],
    }  # fmt: skip


class FakeRunner:
    """Stands in for Randovania: fast, deterministic, and controllable."""

    def __init__(self) -> None:
        self.requests: list[GenerateRequest] = []
        self.exports: list[ExportRequest] = []
        self.release = asyncio.Event()
        self.release.set()
        self.started = asyncio.Event()
        self.fail_with: str | None = None
        self.progress_lines = ["Creating item pool", "Resolving... 1 total resources"]

    async def generate(
        self, request: GenerateRequest, workdir: Path, on_progress: ProgressCallback
    ) -> GenerateResult:
        self.requests.append(request)
        self.started.set()
        for line in self.progress_lines:
            on_progress(line)
        await self.release.wait()
        if self.fail_with:
            raise RunnerError(self.fail_with)
        out = workdir / "seed.rdvgame"
        out.write_text(json.dumps(fake_rdvgame(request.race)))
        return GenerateResult(permalink="FAKEPERMALINK", seed_hash="HASH1234", rdvgame=out)

    async def export(
        self, request: ExportRequest, workdir: Path, on_progress: ProgressCallback
    ) -> ExportResult:
        """Writes a mod shaped like open-dread-rando's Atmosphere output (no game data)."""
        self.exports.append(request)
        self.started.set()
        on_progress("[ 50%] Patching")
        await self.release.wait()
        if self.fail_with:
            raise RunnerError(self.fail_with)
        mod = workdir / "mod"
        write_fake_mod(mod)
        return ExportResult(mod=mod)


def write_fake_mod(mod: Path) -> None:
    files = {
        "contents/010093801237c000/romfs/packs/system/system.pkg": b"P" * 1000,
        "contents/010093801237c000/exefs/main.npdm": b"N" * 10,
        "exefs_patches/DreadRandovania/ABCDEF.ips": b"IPS",
        "patcher.json": b"{}",  # written by the real patcher; must be dropped
    }
    for name, data in files.items():
        path = mod / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


FAKE_MOD_BYTES = 1000 + 10 + 3
FAKE_MOD_FILES = 3


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def queue(db: Database, fake_runner: FakeRunner) -> JobQueue:
    return JobQueue(
        db, fake_runner, db.path.parent, randovania_version="12.0.0", progress_interval=0
    )


@pytest.fixture
async def worker(queue: JobQueue) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(queue.run_worker())
    yield task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def wait_until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


# ---- a stand-in Switch: a local FTP server serving a folder like an SD card ----


@pytest.fixture
def sd_card(tmp_path: Path) -> Path:
    root = tmp_path / "sd"
    (root / "atmosphere" / "contents").mkdir(parents=True)
    return root


class FtpdLikeHandler(FTPHandler):
    """Answers like mtheall's ftpd where it differs from pyftpdlib.

    ftpd replies "450" (a temporary error), not "550", when RETR names a file
    that doesn't exist (``FtpSession::xferFile``).
    """

    def ftp_RETR(self, file: str) -> None:
        if not self.fs.isfile(file):
            self.respond("450 No such file or directory")
            return
        super().ftp_RETR(file)


class BlockedTransfersHandler(FtpdLikeHandler):
    """As if a firewall blocked ftpd's passive ports: PASV names a closed port."""

    closed_port = 0

    def ftp_PASV(self, line: str) -> None:
        high, low = divmod(self.closed_port, 256)
        self.respond(f"227 Entering passive mode (127,0,0,1,{high},{low}).")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve_ftp(root: Path, base: type[FTPHandler] = FtpdLikeHandler) -> Iterator[dict]:
    """Serve root over FTP: anonymous, plus user "samus" with password "secret"."""
    authorizer = DummyAuthorizer()
    with warnings.catch_warnings():
        # ftpd lets anyone write by default, so the stand-in does too.
        warnings.simplefilter("ignore", RuntimeWarning)
        authorizer.add_anonymous(str(root), perm="elradfmwMT")
    authorizer.add_user("samus", "secret", str(root), perm="elradfmwMT")
    handler = type(
        "Handler",
        (base,),
        {"authorizer": authorizer, "banner": "test ftpd", "closed_port": free_port()},
    )
    server = ThreadedFTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.05}, daemon=True)
    thread.start()
    try:
        yield {"host": "127.0.0.1", "port": server.socket.getsockname()[1], "root": root}
    finally:
        server.close_all()
        thread.join(timeout=5)


@pytest.fixture
def ftpd(sd_card: Path) -> Iterator[dict]:
    """A local FTP server that answers like ftpd."""
    with serve_ftp(sd_card) as server:
        yield server


@pytest.fixture
def ftpd_blocked(sd_card: Path) -> Iterator[dict]:
    """ftpd behind a firewall that allows only its own port, not file transfers."""
    with serve_ftp(sd_card, BlockedTransfersHandler) as server:
        yield server
