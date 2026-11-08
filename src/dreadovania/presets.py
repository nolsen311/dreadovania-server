# SPDX-License-Identifier: GPL-3.0-or-later
"""Presets: Randovania's bundled Dread presets and the user's imported ones.

Preset IDs are the bundled preset's file name (``starter_preset``) or
``user:<uuid>`` for an imported one. Imported presets are owner-scoped.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import randovania

from dreadovania.db import Database, utc_now
from dreadovania.validation import InvalidInput

USER_PREFIX = "user:"
MAX_PRESET_BYTES = 1024 * 1024
MAX_NAME = 100
MAX_DESCRIPTION = 2000
_USER_ID = re.compile(r"user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    path: Path


@dataclass(frozen=True)
class PresetInfo:
    id: str
    name: str
    description: str
    builtin: bool


@cache
def bundled_dread_presets() -> dict[str, Preset]:
    """Keyed by file name without extension, e.g. ``starter_preset``."""
    folder = Path(randovania.__file__).parent / "games" / "dread" / "presets"
    presets = {}
    for path in sorted(folder.glob("*.rdvpreset")):
        data = json.loads(path.read_text(encoding="utf-8"))
        presets[path.stem] = Preset(key=path.stem, name=str(data.get("name", path.stem)), path=path)
    return presets


@cache
def _bundled_info() -> list[PresetInfo]:
    infos = []
    for key, preset in bundled_dread_presets().items():
        data = json.loads(preset.path.read_text(encoding="utf-8"))
        infos.append(PresetInfo(key, preset.name, str(data.get("description", "")), True))
    return infos


def is_user_preset(preset_id: str) -> bool:
    return preset_id.startswith(USER_PREFIX)


def parse_preset(raw: bytes) -> dict:
    """Validate an uploaded .rdvpreset. Randovania checks the rest at generation."""
    if len(raw) > MAX_PRESET_BYTES:
        raise InvalidInput("presets can be at most 1 MB")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidInput("that file isn't a Randovania preset (not valid JSON)") from None
    if not isinstance(data, dict) or not isinstance(data.get("configuration"), dict):
        raise InvalidInput("that file isn't a Randovania preset")
    if data.get("game") != "dread":
        raise InvalidInput("only Metroid Dread presets are supported")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
        raise InvalidInput(f"the preset needs a name of 1-{MAX_NAME} characters")
    description = data.get("description", "")
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
        raise InvalidInput(f"the preset description can be at most {MAX_DESCRIPTION} characters")
    return data


class PresetStore:
    def __init__(self, db: Database):
        self.db = db

    def list(self, owner_id: str) -> list[PresetInfo]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, description FROM presets WHERE owner_id = ? ORDER BY created_at",
                (owner_id,),
            ).fetchall()
        user = [PresetInfo(USER_PREFIX + r["id"], r["name"], r["description"], False) for r in rows]
        return [*_bundled_info(), *user]

    def get(self, owner_id: str, preset_id: str) -> PresetInfo | None:
        if not is_user_preset(preset_id):
            return next((p for p in _bundled_info() if p.id == preset_id), None)
        if not _USER_ID.fullmatch(preset_id):
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id, name, description FROM presets WHERE id = ? AND owner_id = ?",
                (preset_id.removeprefix(USER_PREFIX), owner_id),
            ).fetchone()
        return (
            PresetInfo(USER_PREFIX + row["id"], row["name"], row["description"], False)
            if row
            else None
        )

    def content(self, owner_id: str, preset_id: str) -> str | None:
        """The stored JSON of an imported preset (owner-scoped)."""
        if not _USER_ID.fullmatch(preset_id):
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT content FROM presets WHERE id = ? AND owner_id = ?",
                (preset_id.removeprefix(USER_PREFIX), owner_id),
            ).fetchone()
        return row["content"] if row else None

    def import_preset(self, owner_id: str, raw: bytes) -> PresetInfo:
        data = parse_preset(raw)
        preset_id = str(uuid.uuid4())
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO presets (id, owner_id, name, description, content, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (preset_id, owner_id, data["name"].strip(), data.get("description", ""),
                 json.dumps(data), utc_now()),
            )  # fmt: skip
        return PresetInfo(
            USER_PREFIX + preset_id, data["name"].strip(), data.get("description", ""), False
        )

    def delete(self, owner_id: str, preset_id: str) -> bool:
        if not _USER_ID.fullmatch(preset_id):
            return False
        with self.db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM presets WHERE id = ? AND owner_id = ?",
                (preset_id.removeprefix(USER_PREFIX), owner_id),
            )
        return cur.rowcount > 0
