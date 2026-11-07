# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from conftest import make_user
from fastapi.testclient import TestClient

from dreadovania.app import create_app
from dreadovania.config import Settings
from dreadovania.db import Database
from dreadovania.jobs import INTERRUPTED, JobQueue


def test_startup_migrates_recovers_and_runs_the_worker(tmp_path: Path) -> None:
    data = tmp_path / "data"
    # A job left "running" by a previous process that crashed.
    db = Database(data / "dreadovania.db")
    db.migrate()
    owner = make_user(db, "alice")
    # Submitting only writes rows; no runner is needed until a worker runs.
    stale = JobQueue(db, None, data, "x").submit_generate(owner, preset_key="starter_preset")  # type: ignore[arg-type]
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (stale.job_id,))

    app = create_app(Settings(data_dir=data))
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert app.state.db.schema_version() == app.state.db.latest_version
        job = app.state.jobs.get_job(stale.job_id, owner)
        assert (job["status"], job["error"]) == ("failed", INTERRUPTED)
    # Leaving the context runs shutdown: the worker stops without errors.


def test_app_without_lifespan_still_serves_static_routes() -> None:
    client = TestClient(create_app())  # no "with": lifespan not run, nothing written
    assert client.get("/healthz").status_code == 200
