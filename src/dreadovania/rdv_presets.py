# SPDX-License-Identifier: GPL-3.0-or-later
"""Editing Dread presets with Randovania's own definitions (ADR 0019).

The one module that knows Randovania's preset internals (ADR 0002): loading
and migrating a preset, the settings the editor shows, applying a tab's form,
and checking the result the way Randovania does.

Labels and help text follow Randovania's own preset editor (its ``.ui`` files
and preset tabs, GPL-3.0-or-later, (c) the Randovania developers), so its
guides and community advice apply here too. Settings Randovania's desktop app
doesn't show have plain wording of our own, marked ``ours=True``.
"""

from __future__ import annotations

import dataclasses
import enum
import html
import re
import typing
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from randovania.game.game_enum import RandovaniaGame
from randovania.game_description import default_database
from randovania.game_description.pickup.pickup_entry import StartingPickupBehavior
from randovania.games.dread.layout import progressive_items as dread_progressive_items
from randovania.games.dread.layout.dread_configuration import (
    DreadConfiguration,
    DreadRavenBeakDamageMode,
)
from randovania.generator.pickup_pool import pool_creator
from randovania.layout.base.available_locations import RandomizationMode
from randovania.layout.base.damage_strictness import LayoutDamageStrictness
from randovania.layout.base.logical_pickup_placement_configuration import (
    LogicalPickupPlacementConfiguration,
)
from randovania.layout.base.logical_resource_action import LayoutLogicalResourceAction
from randovania.layout.base.pickup_model import PickupModelDataSource, PickupModelStyle
from randovania.layout.base.standard_pickup_configuration import StandardPickupConfiguration
from randovania.layout.base.standard_pickup_state import (
    DEFAULT_MAXIMUM_SHUFFLED,
    StandardPickupPriority,
    StandardPickupState,
    StandardPickupStateCase,
)
from randovania.layout.generator_parameters import GeneratorParameters
from randovania.layout.permalink import Permalink
from randovania.layout.preset import Preset
from randovania.layout.versioned_preset import VersionedPreset


class PresetProblem(Exception):
    """Randovania refused the preset or a change. The message is for the user."""


# ---- loading, saving, checking --------------------------------------------------------------


def load(data: dict) -> Preset:
    """A preset from its JSON, migrated to Randovania's current schema."""
    try:
        preset = VersionedPreset(data, from_layout_description=False).get_preset()
    except Exception as exc:  # Randovania raises many kinds; all mean "not usable"
        raise PresetProblem(f"Randovania can't read this preset: {_reason(exc)}") from None
    if not isinstance(preset.configuration, DreadConfiguration):
        raise PresetProblem("only Metroid Dread presets are supported")
    return preset


def to_json(preset: Preset) -> dict:
    """The preset as a ``.rdvpreset`` in Randovania's current schema."""
    return VersionedPreset.with_preset(preset).as_json


def copy_of(source: Preset, name: str) -> Preset:
    """A new preset with the same settings, as Randovania's "Customize" does: a new
    name and UUID. (Randovania 12 no longer records which preset it came from.)"""
    return dataclasses.replace(source, name=name, uuid=uuid.uuid4())


_BREAK = re.compile(r"<br\s*/?>|</p>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")


def description_text(description: str) -> str:
    """Randovania's descriptions are rich text (``<br/>`` for line breaks): plain lines."""
    text = _TAG.sub("", _BREAK.sub("\n", description))
    return html.unescape(text).strip()


def description_markup(text: str) -> str:
    """Plain lines back into Randovania's form, so its desktop app shows the breaks."""
    lines = [html.escape(line.rstrip(), quote=False) for line in text.strip().splitlines()]
    return "<br/>".join(lines)


def renamed(preset: Preset, name: str, description: str) -> Preset:
    return dataclasses.replace(preset, name=name, description=description)


def check(preset: Preset) -> list[str]:
    """Pack the preset into a permalink, as generating does. Returns Randovania's warnings."""
    pool = pool_size(preset)
    if pool.pickups > pool.locations:
        raise PresetProblem(
            f"there are {pool.pickups} pickups for {pool.locations} locations: "
            "shuffle fewer items or expansions"
        )
    try:
        Permalink.from_parameters(
            GeneratorParameters(seed_number=0, spoiler=True, presets=[preset])
        )
    except Exception as exc:
        raise PresetProblem(f"Randovania can't use these settings: {_reason(exc)}") from None
    return warnings(preset)


def warnings(preset: Preset) -> list[str]:
    """Randovania's own warnings: dangerous settings and unsupported features."""
    config = preset.configuration
    return [*config.dangerous_settings(), *config.unsupported_features()]


def _reason(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) <= 300 else text[:297] + "..."


# ---- what the editor shows ------------------------------------------------------------------

Kind = Literal["bool", "int", "optional_int", "choice"]


@dataclass(frozen=True)
class Choice:
    value: str
    label: str


@dataclass(frozen=True)
class Setting:
    path: tuple[str, ...]  # into DreadConfiguration, e.g. ("artifacts", "required_artifacts")
    label: str
    kind: Kind
    help: str = ""
    choices: tuple[Choice, ...] = ()
    suffix: str = ""
    experimental: bool = False  # hidden as experimental in Randovania's app
    ours: bool = False  # not in Randovania's app; our wording

    @property
    def key(self) -> str:
        """The form field name."""
        return ".".join(self.path)

    @property
    def bounds(self) -> tuple[int | None, int | None]:
        meta = _field(self.path).metadata
        return meta.get("min"), meta.get("max")


@dataclass(frozen=True)
class Group:
    title: str
    settings: tuple[Setting, ...]
    help: str = ""


@dataclass(frozen=True)
class Tab:
    id: str
    title: str
    groups: tuple[Group, ...] = ()
    help: str = ""
    custom: str = ""  # a tab with its own form and template, e.g. "pickups"
    covers: tuple[tuple[str, ...], ...] = ()  # what a custom tab edits, for coverage()

    @property
    def settings(self) -> list[Setting]:
        return [s for g in self.groups for s in g.settings]

    @property
    def ready(self) -> bool:
        return bool(self.groups) or bool(self.custom)


def _choices(enum_type: type[enum.Enum], labels: Mapping[Any, str] | None = None) -> tuple:
    return tuple(
        Choice(str(m.value), (labels or {}).get(m, getattr(m, "long_name", None) or str(m.value)))
        for m in enum_type
    )


_DAMAGE = "damage per second"

# In the order of Randovania's preset editor (dread_preset_tabs). Tabs without
# groups are coming in later pull requests (ADR 0019).
TABS: tuple[Tab, ...] = (
    Tab("logic", "Trick level"),
    Tab("generation", "Generation", (
        Group("Item placement", (
            Setting(("available_locations", "randomization_mode"), "Item placement", "choice",
                    "If major/minor split is enabled, major items (e.g., major upgrades and keys "
                    "to new areas) and minor items (i.e, ammo expansions) will be shuffled "
                    "separately. Minor items in excess of the number of minor locations will be "
                    "placed in major locations, but major items won't be placed in minor "
                    "locations.",
                    choices=_choices(RandomizationMode, {
                        RandomizationMode.FULL: "Full",
                        RandomizationMode.MAJOR_MINOR_SPLIT: "Major/minor split",
                    })),
            Setting(("first_progression_must_be_local",),
                    "Require first progression to be local", "bool",
                    "Ensures that your first progression is placed in your own world, ensuring "
                    "you always have something to do before potentially having to wait for "
                    "progression. This only changes anything in multiworld sessions. Warning: "
                    "this will make generation fail more frequently.",
                    experimental=True),
            Setting(("logical_pickup_placement",), "Logical pickup placement", "choice",
                    "Minimal: only the pickups required to beat the game are guaranteed to be "
                    "obtainable.",
                    choices=_choices(LogicalPickupPlacementConfiguration), experimental=True),
        )),
        Group("Logic settings", (
            Setting(("logical_resource_action",), "Dangerous actions", "choice",
                    "A dangerous action is the act of moving past a lock without the "
                    "appropriate items needed to head backwards, or doing an action that can "
                    "only be done once. Randomly: dangerous actions might be required by logic. "
                    "Last resort: only allows dangerous actions to be required if no other "
                    "option is available for progression. Warning: due to how item placement "
                    "works, certain locations will have progression extremely less often or "
                    "even never.",
                    choices=_choices(LayoutLogicalResourceAction)),
            Setting(("allow_highly_dangerous_logic",), "Allow highly dangerous logic", "bool",
                    "Highly dangerous logic enables dangerous actions that are not as obvious. "
                    "Using these dangerous actions could render a seed unbeatable if the "
                    "player were to save afterwards."),
            Setting(("trick_level", "minimal_logic"), "Use minimal logic", "bool",
                    "Minimal logic checks for the bare minimum items to create an almost pure "
                    "random layout. It assumes the player has extensive knowledge of the game "
                    "and will likely require major glitches (e.g. breaking out of bounds) to "
                    "complete. There are no guarantees that a seed will be possible in this "
                    "case."),
            Setting(("check_if_beatable_after_base_patches",),
                    "Verify if game is beatable after deciding starting location, elevator, "
                    "etc.", "bool"),
        )),
        Group("Damage strictness", (
            Setting(("damage_strictness",), "Damage strictness", "choice",
                    "Certain locations, such as rooms with bosses or passive damage over time, "
                    "require you to be able to take a certain amount of damage. This setting "
                    "controls how much energy the logic will expect you to have to reach these "
                    "locations.",
                    choices=_choices(LayoutDamageStrictness)),
        )),
    )),
    Tab("hints", "Hints"),
    Tab("locations", "Location pool"),
    Tab("goal", "Goal", (
        Group("Metroid DNA", (
            Setting(("artifacts", "required_artifacts"), "Required Metroid DNA", "int",
                    "In addition to just reaching Itorash, it's now necessary to collect "
                    "Metroid DNA in order to reach Raven Beak. A Navigation Station has been "
                    "added to Itorash entrance that tells you where the DNA can be found."),
        )),
        Group("Placement", (
            Setting(("artifacts", "prefer_emmi"), "Prefer E.M.M.I.", "bool"),
            Setting(("artifacts", "prefer_major_bosses"),
                    "Prefer major bosses (Corpius, Drogyga, Escue, Experiment Z-57, Golzuna "
                    "and Kraid)", "bool"),
        ), help="These options limit where Metroid DNA will be placed. There can only be as "
                "many DNA shuffled as there are preferred locations enabled. Each option adds "
                "6 locations, up to a total of 12."),
    )),
    Tab("pickups", "Pickup pool", custom="pickups", covers=(
        ("standard_pickup_configuration",), ("ammo_pickup_configuration",),
        ("pickup_model_style",), ("pickup_model_data_source",),
    )),
    Tab("transporters", "Transporters"),
    Tab("start", "Starting area"),
    Tab("doors", "Doors"),
    Tab("energy", "Energy", (
        Group("Energy Part", (
            Setting(("immediate_energy_parts",), "Immediate Energy Part", "bool",
                    "When enabled, Energy Fragments immediately increase your maximum energy "
                    "by 1/4 of the amount an Energy Tank would."),
        )),
        Group("Energy tank", (
            Setting(("energy_per_tank",), "Energy per tank", "int",
                    "How much energy each Energy Tank you collect will provide. Your base "
                    "energy is always this quantity, minus 1. While logic will respect this "
                    "value, only the original value (100) has been tested. The value can only "
                    "be changed when Immediate Energy Part is enabled.",
                    suffix="energy"),
        )),
        Group("Environmental damage", (
            Setting(("constant_heat_damage",), "Use constant heat damage", "optional_int",
                    suffix=_DAMAGE),
            Setting(("constant_cold_damage",), "Use constant cold damage", "optional_int",
                    suffix=_DAMAGE),
            Setting(("constant_lava_damage",), "Use constant lava damage", "optional_int",
                    suffix=_DAMAGE),
        ), help="When enabled, cold and heated rooms, or lava, will deal a fixed damage per "
                "second instead of the normal behavior of scaling up over time. Logic will "
                "never require heat/cold runs."),
    )),
    Tab("other", "Other", (
        Group("Unlocking access", (
            Setting(("hanubia_shortcut_no_grapple",),
                    "Remove Grapple Blocks in Hanubia - Ferenia Shortcut", "bool",
                    "Hanubia - Ferenia Shortcut, the room next to the elevator to Ferenia in "
                    "Hanubia, has two Grapple Blocks that prevent access to Hanubia from this "
                    "elevator."),
            Setting(("hanubia_easier_path_to_itorash",),
                    "Remove Grapple and Wave locks in path to Itorash", "bool",
                    "Removes the Grapple Blocks and Wave Beam door locks on the top of Hanubia."),
        )),
        Group("Bosses", (
            Setting(("raven_beak_damage_table_handling",), "Raven Beak damage resistance",
                    "choice",
                    "Normally, Raven Beak has a strong resistance to only Wave Beam and Ice "
                    "Missiles. Unmodified keeps that. Consistent, with damage reduction: the "
                    "damage of all weapons is reduced by the same factor as Wave Beam and Ice "
                    "Missiles. Consistent, without damage reduction: the damage of Wave Beam "
                    "and Ice Missiles is increased to match that of Plasma Beam and Super "
                    "Missiles.",
                    choices=_choices(DreadRavenBeakDamageMode)),
        )),
        Group("X Parasites", (
            Setting(("x_starts_released",), "Start game with the X already released", "bool",
                    "The X variant of enemies are stronger, making the game harder. This "
                    "allows access to Golzuna and Experiment Z-57 without having to visit "
                    "Elun."),
        )),
        Group("Miscellaneous", (
            Setting(("nerf_power_bombs",), "Power Bomb limitations", "bool",
                    "The Power Bomb is one of the most powerful abilities in the game, as it "
                    "can defeat most enemies and some bosses in one hit, as well as open "
                    "Charge Beam doors, and destroy Enkies. This setting removes the ability to "
                    "destroy Enkies and open Charge Beam doors, making Ice Missiles and Charge "
                    "Beam much more valuable."),
            Setting(("skip_item_popups",), "Skip item acquisition popups", "bool",
                    "The popup dialogs shown when collecting pickups are skipped. Instead, a "
                    "message is shown near the top of the screen for several seconds after "
                    "collecting an item."),
            Setting(("warp_to_start",), "Warp to start", "bool",
                    "Adds a way to warp back to the starting location from save stations. "
                    "Randovania's app doesn't show this setting for Dread; its presets turn "
                    "it on.", ours=True),
        )),
    )),
    Tab("lights", "Lights", (
        Group("Lights out", tuple(
            Setting(("disabled_lights", region), region.title(), "bool")
            for region in ("artaria", "burenia", "cataris", "dairon", "elun", "ferenia",
                           "ghavoran", "hanubia", "itorash")
        ), help="If a region is enabled, every light source within it will be removed. This "
                "setting makes the game very dark, but still somewhat visible."),
    )),
    Tab("chaos", "Chaos", (
        Group("Miscellaneous", (
            Setting(("freesink",), "Enable Freesink", "bool",
                    "Usually, Samus will die if she is out of bounds, usually due to "
                    "performing floor clips. When checked, this behavior is removed from the "
                    "game and Samus can freely move out of bounds."),
        )),
    ), help="Experimental settings which do not yet have (or will never have) logic support."),
)  # fmt: skip

# Settings that aren't editable here, and why. The coverage test requires every
# DreadConfiguration field to be editable or listed here (ADR 0019 §4).
NOT_EDITABLE_YET: dict[str, str] = {
    "trick_level": "trick levels: the Trick level tab, in a later version",
    "starting_location": "the Starting area tab, in a later version",
    "available_locations": "excluded locations: the Location pool tab, in a later version",
    "dock_weakness_distributor": "the Doors tab, in a later version",
    "hints": "the Hints tab, in a later version",
    "april_fools_hints": "the Hints tab, in a later version",
    "teleporters": "the Transporters tab, in a later version",
}
NOT_OFFERED: dict[str, str] = {
    "single_set_for_pickups_that_solve": "a generator detail Randovania's app doesn't offer",
}
FIXED_SUBFIELDS = frozenset({"game"})  # which game a structure is for: never edited


def _hints(cls: Any) -> dict[str, Any]:
    """A dataclass's field types. Some Randovania types name classes only imported
    for type checking; those resolve to None (they're never descended into)."""
    try:
        return typing.get_type_hints(cls)
    except NameError:
        module = vars(__import__(cls.__module__, fromlist=["_"]))
        hints: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            try:
                hints[f.name] = eval(f.type, module) if isinstance(f.type, str) else f.type  # noqa: S307
            except NameError:
                hints[f.name] = None
        return hints


def tab(tab_id: str) -> Tab | None:
    return next((t for t in TABS if t.id == tab_id), None)


def _field(path: tuple[str, ...]) -> dataclasses.Field:
    cls: Any = DreadConfiguration
    found = dataclasses.fields(cls)[0]  # replaced below: paths are never empty
    for name in path:
        found = next(f for f in dataclasses.fields(cls) if f.name == name)
        cls = _hints(cls)[name]
    return found


def _get(config: Any, path: tuple[str, ...]) -> Any:
    for name in path:
        config = getattr(config, name)
    return config


def _set(config: Any, path: tuple[str, ...], value: Any) -> Any:
    if len(path) == 1:
        return dataclasses.replace(config, **{path[0]: value})
    inner = _set(getattr(config, path[0]), path[1:], value)
    return dataclasses.replace(config, **{path[0]: inner})


def form_value(preset: Preset, setting: Setting) -> Any:
    """The current value, as the form shows it: bool, int, int or None, or a choice's value."""
    value = _get(preset.configuration, setting.path)
    if setting.kind == "choice":
        return str(value.value)
    return value


def apply(preset: Preset, tab_id: str, form: Mapping[str, str]) -> Preset:
    """The preset with one tab's form applied. Randovania checks the bounds."""
    found = tab(tab_id)
    if found is None or not found.ready:
        raise PresetProblem("that settings tab doesn't exist")
    if found.custom == "pickups":
        return apply_pickups(preset, form)
    config = preset.configuration
    for setting in found.settings:
        config = _set(config, setting.path, _parse(setting, config, form))
    try:
        return dataclasses.replace(preset, configuration=config)
    except ValueError as exc:
        raise PresetProblem(_reason(exc)) from None


def _parse(setting: Setting, config: Any, form: Mapping[str, str]) -> Any:
    raw = form.get(setting.key, "").strip()
    if setting.kind == "bool":
        return setting.key in form and raw not in ("", "0", "false", "off")
    if setting.kind == "choice":
        current = _get(config, setting.path)
        for member in type(current):
            if str(member.value) == raw:
                return member
        raise PresetProblem(f"{setting.label}: choose one of the options")
    if setting.kind == "optional_int":
        if f"{setting.key}.on" not in form:
            return None
        raw = form.get(f"{setting.key}.value", "").strip()
    try:
        number = int(raw)
    except ValueError:
        raise PresetProblem(f"{setting.label}: enter a whole number") from None
    low, high = setting.bounds
    if (low is not None and number < low) or (high is not None and number > high):
        raise PresetProblem(f"{setting.label}: enter a number from {low} to {high}")
    return number


# ---- the pickup pool (Randovania's Pickup pool tab) -------------------------------------------

_PICKUP_HELP = (
    "Excluded: not in the game. Vanilla: in its original place. Starting: you start with "
    "it. Shuffled: placed somewhere random. Custom: choose the numbers yourself."
)
_CUSTOM = StandardPickupStateCase.CUSTOM


@dataclass(frozen=True)
class AmmoField:
    key: str
    label: str
    value: int
    low: int
    high: int


@dataclass(frozen=True)
class PickupRow:
    name: str
    case: str
    cases: tuple[Choice, ...]
    shuffled: int
    starting: int
    starting_max: int
    original: bool | None  # None: it has no original location
    priority: str
    ammo: tuple[AmmoField, ...]
    locked: bool  # must always be a starting item


@dataclass(frozen=True)
class PickupGroup:
    title: str
    rows: tuple[PickupRow, ...]


@dataclass(frozen=True)
class Progressive:
    name: str  # the progressive pickup, e.g. "Progressive Beam"
    label: str
    on: bool


@dataclass(frozen=True)
class ExpansionRow:
    name: str
    pickups: int
    ammo: tuple[AmmoField, ...]
    requires_main: bool | None  # None: not offered for this expansion


@dataclass(frozen=True)
class PoolSize:
    pickups: int
    locations: int


@dataclass(frozen=True)
class PickupView:
    pool: PoolSize
    groups: tuple[PickupGroup, ...]
    progressive: tuple[Progressive, ...]
    expansions: tuple[ExpansionRow, ...]
    random_starting: tuple[int, int, int]  # minimum, maximum, upper bound
    model_style: Setting
    model_source: Setting
    help: str = _PICKUP_HELP


PRIORITIES = tuple(Choice(p.value, p.pretty_text) for p in StandardPickupPriority)
SHUFFLED_MAX = DEFAULT_MAXIMUM_SHUFFLED[-1]
_MODEL_STYLE = Setting(("pickup_model_style",), "Pickup model", "choice",
                       "How pickups look in the game before you collect them.",
                       choices=_choices(PickupModelStyle))  # fmt: skip
_MODEL_SOURCE = Setting(("pickup_model_data_source",), "Model data source", "choice",
                        "When pickups are hidden, what they look like instead.",
                        choices=_choices(PickupModelDataSource))  # fmt: skip


def _db() -> Any:
    return default_database.pickup_database_for_game(RandovaniaGame.METROID_DREAD)


def _capacity(item: str) -> tuple[str, int]:
    resource = default_database.resource_database_for(RandovaniaGame.METROID_DREAD).get_item(item)
    return resource.long_name, resource.max_capacity


def _progressive_pairs() -> list[tuple[str, tuple[str, ...]]]:
    return list(dread_progressive_items.tuples())


def _has_item(state: StandardPickupState) -> bool:
    return bool(
        state.num_shuffled_pickups
        or state.num_included_in_starting_pickups
        or state.include_copy_in_original_location
    )


def _progressive_on(config: Any, progressive: str, singles: tuple[str, ...]) -> bool:
    """As Randovania's checkbox: on unless any of the separate pickups is in the game."""
    states = {p.name: s for p, s in config.standard_pickup_configuration.pickups_state.items()}
    return not any(_has_item(states[name]) for name in singles)


def _cases(pickup: Any) -> tuple[Choice, ...]:
    """The cases Randovania's app offers for this pickup, with its wording."""
    out = []
    for case in StandardPickupStateCase:
        if case == StandardPickupStateCase.VANILLA and not pickup.original_locations:
            continue
        if case == StandardPickupStateCase.STARTING_ITEM and len(pickup.progression) > 1:
            continue
        if case == StandardPickupStateCase.SHUFFLED and pickup.count_for_shuffled_case > 1:
            text = f"{pickup.count_for_shuffled_case} shuffled copies"
        elif case == StandardPickupStateCase.STARTING_ITEM and pickup.count_for_starting_case > 1:
            text = f"{pickup.count_for_starting_case} starting copies"
        else:
            text = case.pretty_text
        out.append(Choice(case.value, text))
    return tuple(out)


def _starting_max(pickup: Any) -> int:
    if len(pickup.progression) > 1:
        return 0
    return min((_capacity(p)[1] for p in pickup.progression), default=1)


def pool_size(preset: Preset) -> PoolSize:
    per_category = pool_creator.calculate_pool_pickup_count(preset.configuration)
    total = pool_creator.get_total_pickup_count(per_category)
    return PoolSize(total.pickups, total.locations)


def pickup_view(preset: Preset) -> PickupView:
    config = preset.configuration
    standard = config.standard_pickup_configuration
    hidden: set[str] = set()
    progressive = []
    for name, singles in _progressive_pairs():
        on = _progressive_on(config, name, singles)
        hidden.update(singles if on else (name,))
        progressive.append(Progressive(name, "Use progressive " + " → ".join(singles), on))
    groups: dict[str, list[PickupRow]] = {}
    for pickup, state in standard.pickups_state.items():
        if pickup.hide_from_gui or pickup.name in hidden:
            continue
        ammo = tuple(
            AmmoField(f"pickup.{pickup.name}.ammo.{i}", label, value, 0, high)
            for i, (item, value) in enumerate(zip(pickup.ammo, state.included_ammo, strict=True))
            for label, high in [_capacity(item)]
        )
        groups.setdefault(pickup.gui_category.long_name, []).append(
            PickupRow(
                name=pickup.name,
                case=state.closest_case(pickup).value,
                cases=_cases(pickup),
                shuffled=state.num_shuffled_pickups,
                starting=state.num_included_in_starting_pickups,
                starting_max=_starting_max(pickup),
                original=state.include_copy_in_original_location
                if pickup.original_locations
                else None,
                priority=state.priority.value,
                ammo=ammo,
                locked=pickup.starting_condition == StartingPickupBehavior.MUST_BE_STARTING,
            )
        )
    expansions = []
    for ammo, state in config.ammo_pickup_configuration.pickups_state.items():
        fields = []
        for i, (item, value) in enumerate(zip(ammo.items, state.ammo_count, strict=True)):
            label, high = _capacity(item)
            low = -high if ammo.allows_negative else 0
            fields.append(AmmoField(f"ammo.{ammo.name}.count.{i}", label, value, low, high))
        expansions.append(
            ExpansionRow(
                name=ammo.name,
                pickups=state.pickup_count,
                ammo=tuple(fields),
                requires_main=state.requires_main_item
                if ammo.temporary and ammo.show_requires_main
                else None,
            )
        )
    high = _starting_bound()
    return PickupView(
        pool=pool_size(preset),
        groups=tuple(PickupGroup(title, tuple(rows)) for title, rows in groups.items()),
        progressive=tuple(progressive),
        expansions=tuple(expansions),
        random_starting=(
            standard.minimum_random_starting_pickups,
            standard.maximum_random_starting_pickups,
            high,
        ),
        model_style=_MODEL_STYLE,
        model_source=_MODEL_SOURCE,
    )


def _starting_bound() -> int:
    meta = {f.name: f.metadata for f in dataclasses.fields(StandardPickupConfiguration)}.get(
        "maximum_random_starting_pickups", {}
    )
    return int(meta.get("max", 30))


def _int(form: Mapping[str, str], key: str, label: str, low: int, high: int, default: int) -> int:
    if key not in form:
        return default
    try:
        value = int(form[key].strip())
    except ValueError:
        raise PresetProblem(f"{label}: enter a whole number") from None
    if not low <= value <= high:
        raise PresetProblem(f"{label}: enter a number from {low} to {high}")
    return value


def apply_pickups(preset: Preset, form: Mapping[str, str]) -> Preset:
    """The Pickup pool tab's form applied, checked by Randovania."""
    if "random_starting.min" not in form:  # every field is sent; a partial form would reset
        raise PresetProblem("the form was incomplete: reload the page and try again")
    config = preset.configuration
    standard = config.standard_pickup_configuration
    by_name = {p.name: p for p in standard.pickups_state}
    new_states: dict[Any, StandardPickupState] = {}
    reset: set[str] = set()

    # Switching a progressive group resets its pickups, as Randovania's checkbox does.
    for name, singles in _progressive_pairs():
        wanted = f"progressive.{name}" in form
        if wanted == _progressive_on(config, name, singles):
            continue
        new_states[by_name[name]] = StandardPickupState(
            num_shuffled_pickups=len(singles) if wanted else 0
        )
        for single in singles:
            new_states[by_name[single]] = StandardPickupState(
                num_shuffled_pickups=0 if wanted else 1
            )
        reset.update((name, *singles))

    for pickup, state in standard.pickups_state.items():
        prefix = f"pickup.{pickup.name}"
        if pickup.name in reset or f"{prefix}.case" not in form:
            continue  # reset above, or not shown (hidden by its progressive group)
        ammo = tuple(
            _int(form, f"{prefix}.ammo.{i}", f"{pickup.name}: {label}", 0, high, value)
            for i, (item, value) in enumerate(zip(pickup.ammo, state.included_ammo, strict=True))
            for label, high in [_capacity(item)]
        )
        try:
            case = StandardPickupStateCase(form[f"{prefix}.case"])
        except ValueError:
            raise PresetProblem(f"{pickup.name}: choose one of the options") from None
        if case == _CUSTOM:
            priority = form.get(f"{prefix}.priority", state.priority.value)
            try:
                new = StandardPickupState(
                    include_copy_in_original_location=f"{prefix}.original" in form
                    and bool(pickup.original_locations),
                    num_shuffled_pickups=_int(form, f"{prefix}.shuffled",
                                              f"{pickup.name}: shuffled", 0, SHUFFLED_MAX,
                                              state.num_shuffled_pickups),
                    num_included_in_starting_pickups=_int(
                        form, f"{prefix}.starting", f"{pickup.name}: starting", 0,
                        _starting_max(pickup), state.num_included_in_starting_pickups),
                    priority=StandardPickupPriority(priority),
                    included_ammo=ammo,
                )  # fmt: skip
            except ValueError:
                raise PresetProblem(f"{pickup.name}: choose one of the options") from None
        else:
            new = StandardPickupState.from_case(pickup, case, ammo)
            if new is None:
                raise PresetProblem(f"{pickup.name} can't be {case.pretty_text.lower()}")
        if new != state:
            new_states[pickup] = new

    ammo_config = config.ammo_pickup_configuration
    new_ammo = {}
    for ammo, state in ammo_config.pickups_state.items():
        prefix = f"ammo.{ammo.name}"
        counts = []
        for i, (item, value) in enumerate(zip(ammo.items, state.ammo_count, strict=True)):
            label, high = _capacity(item)
            low = -high if ammo.allows_negative else 0
            counts.append(
                _int(form, f"{prefix}.count.{i}", f"{ammo.name}: {label}", low, high, value)
            )
        requires = state.requires_main_item
        if ammo.temporary and ammo.show_requires_main and f"{prefix}.pickups" in form:
            requires = f"{prefix}.requires_main" in form
        new = dataclasses.replace(
            state,
            ammo_count=tuple(counts),
            pickup_count=_int(form, f"{prefix}.pickups", f"{ammo.name}: pickups", 0, 999,
                              state.pickup_count),
            requires_main_item=requires,
        )  # fmt: skip
        if new != state:
            new_ammo[ammo] = new

    high = _starting_bound()
    minimum = _int(form, "random_starting.min", "Minimum random starting items", 0, high,
                   standard.minimum_random_starting_pickups)  # fmt: skip
    maximum = _int(form, "random_starting.max", "Maximum random starting items", 0, high,
                   standard.maximum_random_starting_pickups)  # fmt: skip
    if minimum > maximum:
        raise PresetProblem("random starting items: the minimum can't be more than the maximum")

    try:
        standard = dataclasses.replace(
            standard.replace_states(new_states) if new_states else standard,
            minimum_random_starting_pickups=minimum,
            maximum_random_starting_pickups=maximum,
        )
        for ammo, state in new_ammo.items():
            state.check_consistency(ammo)
        changed = dataclasses.replace(
            config,
            standard_pickup_configuration=standard,
            ammo_pickup_configuration=ammo_config.replace_states(new_ammo)
            if new_ammo
            else ammo_config,
        )
        for setting in (_MODEL_STYLE, _MODEL_SOURCE):
            if setting.key in form:
                changed = _set(changed, setting.path, _parse(setting, changed, form))
        return dataclasses.replace(preset, configuration=changed)
    except ValueError as exc:
        raise PresetProblem(_reason(exc)) from None


def coverage() -> dict[str, list[str]]:
    """Which DreadConfiguration fields are editable here, and which aren't (and why)."""
    editable = {s.path for t in TABS for s in t.settings} | {p for t in TABS for p in t.covers}

    def covered(cls: Any, prefix: tuple[str, ...]) -> bool:
        if prefix in editable:
            return True
        below = any(path[: len(prefix)] == prefix for path in editable)
        if not below or not dataclasses.is_dataclass(cls):
            return False
        hints = _hints(cls)
        return all(
            covered(hints[f.name], (*prefix, f.name))
            for f in dataclasses.fields(cls)
            if f.name not in FIXED_SUBFIELDS
        )

    hints = typing.get_type_hints(DreadConfiguration)
    result: dict[str, list[str]] = {"editable": [], "missing": []}
    for f in dataclasses.fields(DreadConfiguration):
        if covered(hints[f.name], (f.name,)):
            result["editable"].append(f.name)
        elif f.name not in NOT_EDITABLE_YET and f.name not in NOT_OFFERED:
            result["missing"].append(f.name)
    return result


__all__ = [
    "NOT_EDITABLE_YET",
    "NOT_OFFERED",
    "TABS",
    "Choice",
    "Group",
    "PickupView",
    "Preset",
    "PresetProblem",
    "Setting",
    "Tab",
    "apply",
    "apply_pickups",
    "check",
    "copy_of",
    "coverage",
    "description_markup",
    "description_text",
    "form_value",
    "load",
    "pickup_view",
    "pool_size",
    "renamed",
    "tab",
    "to_json",
    "warnings",
]
