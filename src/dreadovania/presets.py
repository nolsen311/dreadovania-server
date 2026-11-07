# SPDX-License-Identifier: GPL-3.0-or-later
"""Randovania's bundled Dread presets (user-imported presets arrive in PR 3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import randovania


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    path: Path


@cache
def bundled_dread_presets() -> dict[str, Preset]:
    """Keyed by file name without extension, e.g. ``starter_preset``."""
    folder = Path(randovania.__file__).parent / "games" / "dread" / "presets"
    presets = {}
    for path in sorted(folder.glob("*.rdvpreset")):
        data = json.loads(path.read_text(encoding="utf-8"))
        presets[path.stem] = Preset(key=path.stem, name=str(data.get("name", path.stem)), path=path)
    return presets
