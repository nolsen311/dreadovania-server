# SPDX-License-Identifier: GPL-3.0-or-later
"""Randovania's Dread cosmetic options (ADR 0025): the server's list matches
Randovania's, the adapter applies them, and each patched seed records its own."""

from __future__ import annotations

import dataclasses
import enum
import json
import subprocess
import sys
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from randovania.games.dread.layout.dread_cosmetic_patches import DreadCosmeticPatches
from test_api import H, wait_for_job
from test_patching import api_ready_seed, client, fake_cli, romfs_dir  # noqa: F401 (fixtures)
from test_randovania_cli import PRESET, _randovania

from dreadovania.cosmetics import FIXED, OUR_DEFAULTS, RANDOVANIA_DEFAULTS, Cosmetics
from dreadovania.runner import ExportRequest

# ---- the list matches Randovania's ---------------------------------------------------------


def test_the_options_match_randovanias_exactly() -> None:
    """Fails after a Randovania upgrade that adds, removes or changes an option:
    update ``cosmetics.py`` (and the page) to match, and ADR 0025's table."""
    theirs = {f.name: f for f in dataclasses.fields(DreadCosmeticPatches)}
    ours = Cosmetics.model_fields
    assert set(theirs) == set(ours) | set(FIXED)
    defaults = DreadCosmeticPatches()
    for name, field in ours.items():
        theirs_default = getattr(defaults, name)
        if isinstance(theirs_default, enum.Enum):
            values = {e.value for e in type(theirs_default)}
            assert set(typing.get_args(field.annotation)) == values, name
            theirs_default = theirs_default.value
        else:
            assert field.annotation is type(theirs_default), name
        assert field.default == OUR_DEFAULTS.get(name, theirs_default), name
    for name, value in FIXED.items():
        assert getattr(defaults, name) == value
    assert RANDOVANIA_DEFAULTS.for_randovania() == defaults.as_json


def test_values_are_checked_before_they_reach_randovania() -> None:
    bad_values = [
        {"music_volume": 101}, {"sfx_volume": -1}, {"missile_cosmetic": "RAINBOW"},
        {"alt_bomb": "default"}, {"enable_debug_logging": True}, {"surprise": 1},
    ]  # fmt: skip
    for bad in bad_values:
        with pytest.raises(ValueError):
            Cosmetics.model_validate(bad)
    assert Cosmetics().separate_save_slots  # Dreadovania's one different default


# ---- the adapter, end to end on a real seed (no game files) ---------------------------------


@pytest.fixture(scope="module")
def seed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("seed") / "seed.rdvgame"
    result = _randovania(
        "layout", "generate-from-presets", "--file", PRESET, "--seed-number", "4242", out
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def _adapter(seed: Path, tmp: Path, options: Cosmetics) -> dict:
    (tmp / "cosmetics.json").write_text(json.dumps(options.for_randovania()))
    result = subprocess.run(
        [sys.executable, "-m", "dreadovania.randovania_adapter", "patcher-data",
         "--layout-file", str(seed), "--cosmetics", str(tmp / "cosmetics.json"),
         "--output", str(tmp / "out.json")],
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((tmp / "out.json").read_text())


def test_the_adapter_gives_the_clis_output_with_the_defaults(seed: Path, tmp_path: Path) -> None:
    cli = tmp_path / "cli.json"
    result = _randovania("layout", "patcher-data", "--layout-file", seed, "--output", cli)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _adapter(seed, tmp_path, RANDOVANIA_DEFAULTS) == json.loads(cli.read_text())


def test_the_adapter_applies_the_options(seed: Path, tmp_path: Path) -> None:
    options = Cosmetics(
        separate_save_slots=True, music_volume=40, show_room_names="ALWAYS",
        missile_cosmetic="TRANS", alt_bomb="ALTERNATE", show_death_counter=True,
    )  # fmt: skip
    data = _adapter(seed, tmp_path, options)
    c = data["cosmetic_patches"]
    assert c["split_saves"] is True
    assert c["config"]["SoundSystemATK"]["fMusicVolume"] == pytest.approx(0.4)
    assert c["lua"]["custom_init"]["enable_room_name_display"] == "ALWAYS"
    assert c["lua"]["custom_init"]["enable_death_counter"] is True
    assert c["shield_versions"]["bomb"] == "ALTERNATE"
    assert data["enable_logging"] is False
    # The save slots' key comes from the seed, not the options: patching again keeps saves.
    plain = _adapter(seed, tmp_path, RANDOVANIA_DEFAULTS)
    assert data["configuration_identifier"] == plain["configuration_identifier"]
    assert data["layout_uuid"] == plain["layout_uuid"]


@pytest.mark.anyio
async def test_the_runner_uses_the_adapter_for_cosmetics(tmp_path: Path, fake_cli) -> None:  # noqa: F811
    script, _make = fake_cli
    from dreadovania.runner import RandovaniaRunner

    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(sys.argv[0]).with_name('adapter.txt').write_text(json.dumps(args))\n"
        "pathlib.Path(args[args.index('--output') + 1]).write_text('{}')\n"
    )
    runner = RandovaniaRunner(
        command=[sys.executable, str(script), "ok"], adapter_command=[sys.executable, str(adapter)]
    )
    options = Cosmetics(music_volume=10).for_randovania()
    request = ExportRequest(rdvgame=Path("/s/seed.rdvgame"), romfs=Path("/r"), cosmetics=options)
    await runner.export(request, tmp_path, lambda _l: None)
    args = json.loads((tmp_path / "adapter.txt").read_text())
    assert args == [
        "patcher-data", "--layout-file", "/s/seed.rdvgame",
        "--cosmetics", str(tmp_path / "cosmetics.json"),
        "--output", str(tmp_path / "patcher-data.json"),
    ]  # fmt: skip
    assert json.loads((tmp_path / "cosmetics.json").read_text()) == options
    calls = (script.parent / "calls.txt").read_text().splitlines()
    assert [json.loads(c)[:2] for c in calls] == [["layout", "export-game"]]


# ---- through the app -----------------------------------------------------------------------


@pytest.fixture
def app_client(client: TestClient) -> Iterator[TestClient]:  # noqa: F811
    yield client


def test_api_reads_and_replaces_the_options(app_client: TestClient) -> None:
    c = app_client
    assert c.get("/api/v1/cosmetics").json() == Cosmetics().model_dump()
    r = c.put("/api/v1/cosmetics", headers=H, json={"music_volume": 101})
    assert r.status_code == 422
    r = c.put("/api/v1/cosmetics", headers=H, json={"music_volume": 30})
    assert r.status_code == 200 and r.json()["music_volume"] == 30
    # A replacement: what isn't given goes back to the default.
    assert r.json()["separate_save_slots"] is True
    assert c.get("/api/v1/cosmetics").json()["music_volume"] == 30
    assert c.put("/api/v1/cosmetics", json={}).status_code == 403  # no Origin: CSRF


def test_a_seed_records_its_options_and_is_patched_again_when_they_change(
    app_client: TestClient, fake_runner
) -> None:
    c = app_client
    seed_id = api_ready_seed(c)
    c.put("/api/v1/cosmetics", headers=H, json={"missile_cosmetic": "PRIDE"})
    job = c.post(f"/api/v1/seeds/{seed_id}/mod", headers=H).json()["job_id"]
    assert wait_for_job(c, job)["status"] == "succeeded"
    assert fake_runner.exports[-1].cosmetics["missile_cosmetic"] == "PRIDE"
    assert fake_runner.exports[-1].cosmetics["enable_debug_logging"] is False
    mod = c.get(f"/api/v1/seeds/{seed_id}").json()["mod"]
    assert mod["cosmetics"]["missile_cosmetic"] == "PRIDE" and mod["cosmetics_current"] is True
    r = c.post(f"/api/v1/seeds/{seed_id}/mod", headers=H)
    assert r.status_code == 409 and "already patched" in r.json()["detail"]

    c.put("/api/v1/cosmetics", headers=H, json={"missile_cosmetic": "TRANS"})
    listed = c.get("/api/v1/seeds").json()[0]["mod"]
    assert listed["cosmetics"]["missile_cosmetic"] == "PRIDE"
    assert listed["cosmetics_current"] is False
    assert "Patch again" in c.get(f"/seeds/{seed_id}").text
    r = c.post(f"/api/v1/seeds/{seed_id}/mod", headers=H)
    assert r.status_code == 202, r.text
    assert wait_for_job(c, r.json()["job_id"])["status"] == "succeeded"
    mod = c.get(f"/api/v1/seeds/{seed_id}").json()["mod"]
    assert mod["cosmetics"]["missile_cosmetic"] == "TRANS" and mod["cosmetics_current"] is True
    assert "Patch again" not in c.get(f"/seeds/{seed_id}").text


def test_a_seed_patched_before_counts_as_randovanias_defaults(app_client: TestClient) -> None:
    c = app_client
    seed_id = api_ready_seed(c)
    job = c.post(f"/api/v1/seeds/{seed_id}/mod", headers=H).json()["job_id"]
    assert wait_for_job(c, job)["status"] == "succeeded"
    with c.app.state.db.connect() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE mods SET cosmetics = NULL")  # as migration 0012 leaves it
    mod = c.get(f"/api/v1/seeds/{seed_id}").json()["mod"]
    assert mod["cosmetics"] == RANDOVANIA_DEFAULTS.model_dump()
    assert mod["cosmetics_current"] is False  # separate save slots are on now
    assert c.post(f"/api/v1/seeds/{seed_id}/mod", headers=H).status_code == 202


def test_the_cosmetics_page(app_client: TestClient) -> None:
    c = app_client
    assert 'href="/account/cosmetics"' in c.get("/account").text
    page = c.get("/account/cosmetics").text
    assert "Separate save slots for each seed" in page and "Pride flag" in page
    form = {"separate_save_slots": "on", "alt_power_bomb": "on", "music_volume": "55",
            "sfx_volume": "100", "ambience_volume": "0", "show_room_names": "WITH_FADE",
            "missile_cosmetic": "BISEXUAL", "show_dna_in_hud": "on"}  # fmt: skip
    r = c.post("/account/cosmetics", headers=H, data=form, follow_redirects=False)
    assert r.status_code == 303
    saved = c.get("/api/v1/cosmetics").json()
    assert saved == Cosmetics(
        music_volume=55, ambience_volume=0, show_room_names="WITH_FADE",
        missile_cosmetic="BISEXUAL", show_dna_in_hud=True, enable_auto_tracker=False,
    ).model_dump()  # fmt: skip
    assert "Saved." in c.get("/account/cosmetics?saved=1").text
    r = c.post("/account/cosmetics", headers=H, data=form | {"music_volume": "loud"})
    assert r.status_code == 400 and "whole numbers" in r.text
    assert c.get("/api/v1/cosmetics").json() == saved  # unchanged
