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
    # Top-level fields; a dotted entry is part of one (e.g. hints.specific_pickup_hints).
    listed = {n for n in (*rdv_presets.NOT_EDITABLE_YET, *rdv_presets.NOT_OFFERED) if "." not in n}
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
        if not tab.ready or tab.custom:  # custom tabs: their own tests below
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
    client.post(f"/presets/{pid}/edit/about", data={"name": "N", "description": "a\nb"}, headers=HX)
    assert exported(client, pid)["description"] == "a<br/>b"
    r = client.post(f"/presets/{pid}/edit/about", data={"name": " "}, headers=HX)
    assert "needs a name" in r.text


def test_bundled_presets_have_no_editor(client: TestClient) -> None:
    assert client.get("/presets/starter_preset/edit").status_code == 404
    r = client.post("/presets/starter_preset/edit/energy", data={}, headers=HX)
    assert r.status_code == 404


def test_descriptions_are_shown_as_lines() -> None:
    assert (
        rdv_presets.description_text("One.<br/><br/>Two &amp; <b>three</b>")
        == "One.\n\nTwo & three"
    )
    assert (
        rdv_presets.description_markup("One.\n\nTwo & <b>") == "One.<br/><br/>Two &amp; &lt;b&gt;"
    )


def test_the_presets_page_has_no_raw_markup(client: TestClient) -> None:
    page = client.get("/presets").text
    assert "&lt;br/&gt;" not in page and "<br/><br/>" not in page


# ---- the pickup pool ---------------------------------------------------------------------------


def pickup_form(preset: rdv_presets.Preset) -> dict[str, str]:
    """The Pickup pool tab's form, as the page shows it."""
    v = rdv_presets.pickup_view(preset)
    form = {f"progressive.{p.name}": "on" for p in v.progressive if p.on}
    for group in v.groups:
        for r in group.rows:
            k = f"pickup.{r.name}"
            form[f"{k}.case"] = r.case
            form[f"{k}.shuffled"] = str(r.shuffled)
            if r.starting_max:
                form[f"{k}.starting"] = str(r.starting)
            if r.original:
                form[f"{k}.original"] = "on"
            form[f"{k}.priority"] = r.priority
            form.update({a.key: str(a.value) for a in r.ammo})
    for e in v.expansions:
        form[f"ammo.{e.name}.pickups"] = str(e.pickups)
        form.update({a.key: str(a.value) for a in e.ammo})
        if e.requires_main:
            form[f"ammo.{e.name}.requires_main"] = "on"
    form["random_starting.min"], form["random_starting.max"] = map(str, v.random_starting[:2])
    for s in (v.model_style, v.model_source):
        form[s.key] = rdv_presets.form_value(preset, s)
    return form


@pytest.mark.parametrize("key", sorted(presets.bundled_dread_presets()))
def test_the_pickup_pool_saves_unchanged(key: str) -> None:
    preset = bundled(key)
    changed = rdv_presets.apply(preset, "pickups", pickup_form(preset))
    assert changed.configuration == preset.configuration


def pickup_state(preset: rdv_presets.Preset, name: str):
    states = preset.configuration.standard_pickup_configuration.pickups_state
    return next(s for p, s in states.items() if p.name == name)


def test_pickup_cases_custom_numbers_and_expansions() -> None:
    preset = bundled()
    form = pickup_form(preset)
    form["pickup.Energy Tank.case"] = "custom"
    form["pickup.Energy Tank.shuffled"] = "3"
    form["pickup.Screw Attack.case"] = "starting_item"
    form["ammo.Missile Tank.pickups"] = "40"
    form["ammo.Missile Tank.count.0"] = "3"
    changed = rdv_presets.apply(preset, "pickups", form)
    assert pickup_state(changed, "Energy Tank").num_shuffled_pickups == 3
    assert pickup_state(changed, "Screw Attack").num_included_in_starting_pickups == 1
    ammo = changed.configuration.ammo_pickup_configuration.pickups_state
    tank = next(s for a, s in ammo.items() if a.name == "Missile Tank")
    assert (tank.pickup_count, tank.ammo_count) == (40, (3,))
    rdv_presets.check(changed)


def test_switching_to_progressive_beams() -> None:
    preset = bundled()
    form = pickup_form(preset)
    assert "progressive.Progressive Beam" not in form  # the starter preset uses separate beams
    form["progressive.Progressive Beam"] = "on"
    changed = rdv_presets.apply(preset, "pickups", form)
    assert pickup_state(changed, "Progressive Beam").num_shuffled_pickups == 3
    assert pickup_state(changed, "Wide Beam").num_shuffled_pickups == 0
    view = rdv_presets.pickup_view(changed)
    shown = {r.name for g in view.groups for r in g.rows}
    assert "Progressive Beam" in shown and "Wide Beam" not in shown


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("ammo.Missile Tank.pickups", "500", "pickups for"),  # more than there are locations
        ("ammo.Missile Tank.count.0", "5000", "from -999 to 999"),
        ("pickup.Missiles.case", "missing", "Missiles"),  # Randovania: must be a starting item
        ("pickup.Energy Tank.case", "nonsense", "choose one of the options"),
        ("random_starting.min", "5", "minimum can't be more"),
    ],
)
def test_refused_pickup_changes(field: str, value: str, reason: str) -> None:
    preset = bundled()
    form = pickup_form(preset)
    form[field] = value
    with pytest.raises(rdv_presets.PresetProblem, match=reason):
        rdv_presets.check(rdv_presets.apply(preset, "pickups", form))


def test_the_pickup_pool_page(client: TestClient) -> None:
    pid = copy(client)["id"]
    page = client.get(f"/presets/{pid}/edit?tab=pickups").text
    assert "Items in pool: 148/149" in page
    assert 'name="pickup.Energy Tank.case"' in page
    assert 'name="progressive.Progressive Suit"' in page
    form = pickup_form(bundled())
    form["ammo.Power Bomb Tank.pickups"] = "10"
    r = client.post(f"/presets/{pid}/edit/pickups", data=form, headers=HX)
    assert "Saved." in r.text, r.text[:500]
    assert "Items in pool: 145/149" in r.text
    config = exported(client, pid)["configuration"]["ammo_pickup_configuration"]
    assert config["pickups_state"]["Power Bomb Tank"]["pickup_count"] == 10


# ---- trick levels and hints ---------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(presets.bundled_dread_presets()))
def test_the_trick_levels_save_unchanged(key: str) -> None:
    preset = bundled(key)
    form = {t.key: t.level for t in rdv_presets.trick_view(preset)}
    assert rdv_presets.apply(preset, "logic", form).configuration == preset.configuration


def test_setting_trick_levels() -> None:
    preset = bundled()
    rows = rdv_presets.trick_view(preset)
    assert len(rows) > 20 and all(r.level == "disabled" for r in rows)  # the starter preset
    assert "Suitless" not in {
        r.key.removeprefix("trick.") for r in rows
    }  # hidden in Randovania too
    form = {r.key: r.level for r in rows}
    form["trick.Movement"] = "advanced"
    changed = rdv_presets.apply(preset, "logic", form)
    levels = {r.key: r.level for r in rdv_presets.trick_view(changed)}
    assert levels["trick.Movement"] == "advanced"
    rdv_presets.check(changed)
    exported_levels = rdv_presets.to_json(changed)["configuration"]["trick_level"][
        "specific_levels"
    ]
    assert exported_levels == {"Movement": "advanced"}
    with pytest.raises(rdv_presets.PresetProblem, match="choose one of the levels"):
        rdv_presets.apply(preset, "logic", {"trick.Movement": "godlike"})


def test_hints() -> None:
    preset = bundled()
    form = {
        "hints.enable_random_hints": "on",
        "hints.minimum_available_locations_for_hint_placement": "7",
        "hints.minimum_location_weight_for_hint_placement": "0.5",
        "april_fools_hints": "on",
    }
    changed = rdv_presets.apply(preset, "hints", form)
    hints = changed.configuration.hints
    assert hints.enable_random_hints and not hints.enable_specific_location_hints
    assert hints.minimum_available_locations_for_hint_placement == 7
    assert hints.minimum_location_weight_for_hint_placement == 0.5
    assert changed.configuration.april_fools_hints
    rdv_presets.check(changed)
    form["hints.minimum_location_weight_for_hint_placement"] = "9"
    with pytest.raises(rdv_presets.PresetProblem, match=r"from 0\.0 to 5\.0"):
        rdv_presets.apply(preset, "hints", form)


def test_the_trick_level_page(client: TestClient) -> None:
    pid = copy(client)["id"]
    page = client.get(f"/presets/{pid}/edit?tab=logic").text
    assert 'name="trick.Movement"' in page and "Ludicrous" in page
    form = {t.key: t.level for t in rdv_presets.trick_view(bundled())}
    form["trick.IBJ"] = "expert"
    r = client.post(f"/presets/{pid}/edit/logic", data=form, headers=HX)
    assert "Saved." in r.text
    assert exported(client, pid)["configuration"]["trick_level"]["specific_levels"] == {
        "IBJ": "expert"
    }
