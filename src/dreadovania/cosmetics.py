# SPDX-License-Identifier: GPL-3.0-or-later
"""Randovania's Dread cosmetic options, per account (ADR 0025).

The fields mirror Randovania's ``DreadCosmeticPatches`` exactly, apart from
``enable_debug_logging`` (emulators only, always off); a test checks the two
still match after a Randovania upgrade. Values are validated here, before they
reach Randovania.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dreadovania.db import Database, utc_now

RoomNames = Literal["NEVER", "ALWAYS", "WITH_FADE"]
MissileColours = Literal[
    "NONE", "PRIDE", "TRANS", "NONBINARY", "ASEXUAL", "BISEXUAL", "PANSEXUAL", "GENDERQUEER"
]
Shield = Literal["DEFAULT", "ALTERNATE"]
Volume = Field(default=100, ge=0, le=100)

# Fields Randovania has that the server doesn't offer, and the value it always uses.
FIXED: dict[str, Any] = {"enable_debug_logging": False}
# Where Dreadovania's default differs from Randovania's (ADR 0025, decision 3).
OUR_DEFAULTS: dict[str, Any] = {"separate_save_slots": True}


class Cosmetics(BaseModel):
    """Cosmetic options: how a patched seed looks and behaves, not where items are."""

    model_config = ConfigDict(extra="forbid")

    show_boss_lifebar: bool = False
    show_enemy_life: bool = False
    show_enemy_damage: bool = False
    show_player_damage: bool = False
    show_dna_in_hud: bool = False
    show_death_counter: bool = False
    enable_auto_tracker: bool = Field(
        default=True, description="Let an item tracker on the network read the game."
    )
    separate_save_slots: bool = Field(
        default=True,
        description="Keep each seed's three save slots apart from other seeds'.",
    )
    music_volume: int = Volume
    sfx_volume: int = Volume
    ambience_volume: int = Volume
    show_room_names: RoomNames = "NEVER"
    missile_cosmetic: MissileColours = "NONE"
    alt_ice_missile: Shield = "DEFAULT"
    alt_storm_missile: Shield = "DEFAULT"
    alt_diffusion_beam: Shield = "DEFAULT"
    alt_bomb: Shield = "DEFAULT"
    alt_cross_bomb: Shield = "DEFAULT"
    alt_power_bomb: Shield = "ALTERNATE"
    alt_closed: Shield = "DEFAULT"

    def for_randovania(self) -> dict[str, Any]:
        """The JSON ``DreadCosmeticPatches.from_json`` takes."""
        return self.model_dump() | FIXED


# The patcher's defaults, which seeds patched before ADR 0025 were patched with.
RANDOVANIA_DEFAULTS = Cosmetics(separate_save_slots=False)

# Labels for the page, in the order shown.
HUD: list[tuple[str, str]] = [
    ("show_boss_lifebar", "Show boss life bars"),
    ("show_enemy_life", "Show enemy life"),
    ("show_enemy_damage", "Show enemy damage"),
    ("show_player_damage", "Show player damage"),
    ("show_dna_in_hud", "Show current and total DNA count in the HUD"),
    ("show_death_counter", "Show a death counter in the HUD"),
    ("enable_auto_tracker", "Allow automatic item trackers"),
]
ROOM_NAMES: dict[str, str] = {
    "NEVER": "Never", "ALWAYS": "Always", "WITH_FADE": "When entering a room",
}  # fmt: skip
MISSILE_COLOURS: dict[str, str] = {
    "NONE": "Vanilla", "PRIDE": "Pride flag", "TRANS": "Trans pride",
    "NONBINARY": "Nonbinary pride", "ASEXUAL": "Asexual pride", "BISEXUAL": "Bisexual pride",
    "PANSEXUAL": "Pansexual pride", "GENDERQUEER": "Genderqueer pride",
}  # fmt: skip
VOLUMES: list[tuple[str, str]] = [
    ("music_volume", "Music"), ("sfx_volume", "Sound effects"),
    ("ambience_volume", "Background ambience"),
]  # fmt: skip
SHIELDS: list[tuple[str, str]] = [
    ("alt_ice_missile", "Ice Missile"), ("alt_storm_missile", "Storm Missile"),
    ("alt_diffusion_beam", "Diffusion Beam"), ("alt_bomb", "Bomb"),
    ("alt_cross_bomb", "Cross Bomb"), ("alt_power_bomb", "Power Bomb"),
    ("alt_closed", "Access permanently closed"),
]  # fmt: skip


def recorded(value: str | None) -> Cosmetics:
    """The options a mod was patched with (NULL: before ADR 0025, the defaults)."""
    if not value:
        return RANDOVANIA_DEFAULTS
    return Cosmetics.model_validate(json.loads(value))


class CosmeticsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, owner_id: str) -> Cosmetics:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT options FROM cosmetics WHERE owner_id = ?", (owner_id,)
            ).fetchone()
        if row is None:
            return Cosmetics()
        return Cosmetics.model_validate(json.loads(row["options"]))

    def set(self, owner_id: str, options: Cosmetics) -> Cosmetics:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO cosmetics (owner_id, options, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (owner_id) DO UPDATE SET options = excluded.options,"
                " updated_at = excluded.updated_at",
                (owner_id, options.model_dump_json(), utc_now()),
            )
        return options
