# SPDX-License-Identifier: GPL-3.0-or-later
"""Editing presets: copies, Randovania's checks, every setting accounted for (ADR 0019)."""

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from randovania.games.dread.layout.dread_configuration import DreadConfiguration
from test_api import H
from test_passkeys import make_client

from dreadovania import presets, rdv_presets

HX = {**H, "HX-Request": "true"}


@pytest.fixture
def client(tmp_path: Path, fake_runner) -> Iterator[TestClient]:
    c = make_client(tmp_path, fake_runner)
    yield c
    c.__exit__(None, None, None)


def copy(client: TestClient, preset_id: str = "starter_preset") -> dict:
    r = client.post(f"/api/v1/presets/{preset_id}/copy", headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def exported(client: TestClient, preset_id: str) -> dict:
    r = client.get(f"/api/v1/presets/{preset_id}/rdvpreset")
    assert r.status_code == 200, r.text
    return r.json()


def bundled(key: str = "starter_preset") -> rdv_presets.Preset:
    path = presets.bundled_dread_presets()[key].path
    return rdv_presets.load(json.loads(path.read_text(encoding="utf-8")))


# ---- every setting -----------------------------------------------------------------------------


def test_every_dread_setting_is_editable_or_accounted_for() -> None:
    result = rdv_presets.coverage()
    assert result["missing"] == []  # a Randovania upgrade added a setting: handle it here
    names = {f.name for f in dataclasses.fields(DreadConfiguration)}
    listed = set(rdv_presets.NOT_EDITABLE_YET) | set(rdv_presets.NOT_OFFERED)
    assert listed <= names  # nothing stale in the lists
    assert not listed & set(result["editable"])  # nothing listed that is now editable
    assert set(result["editable"]) | listed == names


def test_every_setting_has_a_label_and_valid_options() -> None:
    preset = bundled()
    keys = [s.key for t in rdv_presets.TABS for s in t.settings]
    assert len(keys) == len(set(keys))
    for tab in rdv_presets.TABS:
        for s in tab.settings:
            assert s.label, s.key
            value = rdv_presets.form_value(preset, s)  # the path exists
            if s.kind == "choice":
                assert value in {c.value for c in s.choices}, s.key
            if s.kind in ("int", "optional_int"):
                assert s.bounds[0] is not None and s.bounds[1] is not None, s.key


@pytest.mark.parametrize("key", sorted(presets.bundled_dread_presets()))
def test_every_bundled_preset_loads_checks_and_saves_unchanged(key: str) -> None:
    preset = bundled(key)
    rdv_presets.check(preset)
    for tab in rdv_presets.TABS:
        if not tab.ready:
            continue
        form = {}
        for s in tab.settings:
            value = rdv_presets.form_value(preset, s)
            if s.kind == "bool":
                if value:
                    form[s.key] = "on"
            elif s.kind == "optional_int":
                if value is not None:
                    form[f"{s.key}.on"] = "on"
                    form[f"{s.key}.value"] = str(value)
            else:
                form[s.key] = str(value)
        # The form as shown, saved without changes, changes nothing.
        assert rdv_presets.apply(preset, tab.id, form).configuration == preset.configuration
    assert rdv_presets.load(rdv_presets.to_json(preset)).configuration == preset.configuration


# ---- the API ------------------------------------------------------------------------------------


def test_customize_copies_a_bundled_preset(client: TestClient) -> None:
    copied = copy(client)
    assert copied["name"] == "Copy of Starter Preset"
    assert copied["builtin"] is False
    assert copied["id"].startswith("user:")
    data = exported(client, copied["id"])
    assert data["uuid"] != str(bundled().uuid)
    assert [p["id"] for p in client.get("/api/v1/presets").json()][-1] == copied["id"]
    assert client.post("/api/v1/presets/nope/copy", headers=H).status_code == 404


def test_a_whole_preset_can_be_replaced(client: TestClient) -> None:
    copied = copy(client)
    data = exported(client, copied["id"])
    data["name"] = "Big tanks"
    data["configuration"]["energy_per_tank"] = 150
    r = client.put(f"/api/v1/presets/{copied['id']}", json=data, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Big tanks"
    assert isinstance(r.json()["warnings"], list)
    assert exported(client, copied["id"])["configuration"]["energy_per_tank"] == 150


def test_randovania_refuses_bad_settings_and_nothing_changes(client: TestClient) -> None:
    copied = copy(client)
    data = exported(client, copied["id"])
    data["configuration"]["energy_per_tank"] = 5000
    r = client.put(f"/api/v1/presets/{copied['id']}", json=data, headers=H)
    assert r.status_code == 400
    assert "maximum" in r.json()["detail"]
    assert exported(client, copied["id"])["configuration"]["energy_per_tank"] == 100
    r = client.put(f"/api/v1/presets/{copied['id']}", content=b"nope", headers=H)
    assert r.status_code == 400


def test_bundled_presets_cant_be_replaced(client: TestClient) -> None:
    data = exported(client, "starter_preset")
    r = client.put("/api/v1/presets/starter_preset", json=data, headers=H)
    assert r.status_code == 409
    assert "copy it first" in r.json()["detail"]


def test_presets_are_private(client: TestClient, tmp_path: Path, fake_runner) -> None:
    copied = copy(client)
    other = make_client(tmp_path / "other", fake_runner)
    try:
        assert other.get(f"/api/v1/presets/{copied['id']}/rdvpreset").status_code == 404
    finally:
        other.__exit__(None, None, None)


def test_an_edited_preset_generates(client: TestClient) -> None:
    copied = copy(client)
    r = client.post("/api/v1/seeds", json={"preset_id": copied["id"]}, headers=H)
    assert r.status_code == 202, r.text


# ---- the pages ----------------------------------------------------------------------------------


def test_customize_from_the_presets_page(client: TestClient) -> None:
    page = client.get("/presets").text
    assert 'hx-post="/presets/starter_preset/copy"' in page
    r = client.post("/presets/starter_preset/copy", headers=HX)
    assert r.status_code == 204
    target = r.headers["hx-redirect"]
    assert target.startswith("/presets/user:") and target.endswith("/edit")
    page = client.get(target).text
    assert "Copy of Starter Preset" in page
    assert 'aria-current="page"' in page
    assert "Coming next:" in page


@pytest.mark.parametrize("tab", [t.id for t in rdv_presets.TABS if t.ready])
def test_every_tab_shows_its_settings(client: TestClient, tab: str) -> None:
    copied = copy(client)
    page = client.get(f"/presets/{copied['id']}/edit?tab={tab}").text
    for s in rdv_presets.tab(tab).settings:
        assert f'name="{s.key}' in page, s.key


def test_saving_a_tab(client: TestClient) -> None:
    pid = copy(client)["id"]
    form = {
        "immediate_energy_parts": "on",
        "energy_per_tank": "120",
        "constant_heat_damage.on": "on",
        "constant_heat_damage.value": "35",
        "constant_cold_damage.value": "20",  # unticked: off, whatever the number
        "constant_lava_damage.on": "on",
        "constant_lava_damage.value": "20",
    }
    r = client.post(f"/presets/{pid}/edit/energy", data=form, headers=HX)
    assert r.status_code == 200
    assert "Saved." in r.text
    config = exported(client, pid)["configuration"]
    assert config["energy_per_tank"] == 120
    assert config["constant_heat_damage"] == 35
    assert config["constant_cold_damage"] is None


def test_a_refused_change_is_explained_and_not_saved(client: TestClient) -> None:
    pid = copy(client)["id"]
    r = client.post(
        f"/presets/{pid}/edit/goal",
        data={"artifacts.required_artifacts": "13", "artifacts.prefer_emmi": "on"},
        headers=HX,
    )
    assert "Not saved:" in r.text and "from 0 to 12" in r.text
    r = client.post(f"/presets/{pid}/edit/generation", data={"damage_strictness": "9"}, headers=HX)
    assert "choose one of the options" in r.text
    assert exported(client, pid)["configuration"]["artifacts"]["required_artifacts"] == 3


def test_renaming(client: TestClient) -> None:
    pid = copy(client)["id"]
    r = client.post(
        f"/presets/{pid}/edit/about", data={"name": " Nate's ", "description": "Mine"}, headers=HX
    )
    assert r.status_code == 204
    assert client.get(f"/api/v1/presets/{pid}").json()["name"] == "Nate's"
    assert exported(client, pid)["description"] == "Mine"
    r = client.post(f"/presets/{pid}/edit/about", data={"name": " "}, headers=HX)
    assert "needs a name" in r.text


def test_bundled_presets_have_no_editor(client: TestClient) -> None:
    assert client.get("/presets/starter_preset/edit").status_code == 404
    r = client.post("/presets/starter_preset/edit/energy", data={}, headers=HX)
    assert r.status_code == 404
