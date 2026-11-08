# SPDX-License-Identifier: GPL-3.0-or-later
"""Seed history: reading, deleting, purging, and the spoiler (ADR 0011 §3).

Everything is owner-scoped: another user's seed reads as "not found". File
paths are built only from IDs that came out of the database.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dreadovania.db import Database

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_LIST = 200

_SEED_COLUMNS = (
    "s.id, s.status, s.source, s.preset_key, s.preset_name, s.requested_permalink,"
    " s.seed_number, s.race, s.permalink, s.seed_hash, s.randovania_version,"
    " s.created_at, s.completed_at,"
    " (SELECT j.id FROM jobs j WHERE j.seed_id = s.id ORDER BY j.created_at DESC LIMIT 1)"
    " AS job_id"
)


class SeedNotReady(Exception):
    pass


class SeedBusy(Exception):
    pass


class NoSpoiler(Exception):
    pass


def is_uuid(value: str) -> bool:
    return bool(_UUID.fullmatch(value))


class SeedStore:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.seeds_dir = data_dir / "seeds"

    def _dir(self, owner_id: str, seed_id: str) -> Path:
        return self.seeds_dir / owner_id / seed_id

    def list(self, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, MAX_LIST))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_SEED_COLUMNS} FROM seeds s WHERE s.owner_id = ?"  # noqa: S608
                " ORDER BY s.created_at DESC, s.rowid DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, owner_id: str, seed_id: str) -> dict[str, Any] | None:
        if not is_uuid(seed_id):
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT {_SEED_COLUMNS} FROM seeds s WHERE s.id = ? AND s.owner_id = ?",  # noqa: S608
                (seed_id, owner_id),
            ).fetchone()
        return dict(row) if row else None

    def rdvgame_path(self, owner_id: str, seed_id: str) -> Path | None:
        """The stored .rdvgame of a ready seed (owner-scoped)."""
        seed = self.get(owner_id, seed_id)
        if seed is None:
            return None
        if seed["status"] != "ready":
            raise SeedNotReady
        path = self._dir(owner_id, seed["id"]) / "seed.rdvgame"
        return path if path.is_file() else None

    def spoiler(self, owner_id: str, seed_id: str) -> dict[str, Any] | None:
        """Pickup locations and starting state. Race seeds have none."""
        seed = self.get(owner_id, seed_id)
        if seed is None:
            return None
        if seed["race"]:
            raise NoSpoiler
        path = self.rdvgame_path(owner_id, seed_id)
        if path is None:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        worlds = data.get("game_modifications")
        if not data.get("info", {}).get("has_spoiler") or not worlds:
            raise NoSpoiler
        world = worlds[0]
        # Randovania writes the start as "Region/Area/Node" and the starting items
        # as {"pickups": [...]}; normalise both for the API and the UI.
        start = world.get("starting_location")
        parts = start.split("/", 2) if isinstance(start, str) else []
        equipment = world.get("starting_equipment")
        items = equipment.get("pickups", []) if isinstance(equipment, dict) else []
        return {
            "starting_location": (
                dict(zip(("region", "area", "node"), parts, strict=True))
                if len(parts) == 3
                else None
            ),
            "starting_items": [str(i) for i in items],
            "locations": [
                {
                    "region": loc["node_identifier"]["region"],
                    "area": loc["node_identifier"]["area"],
                    "location": loc["node_identifier"]["node"],
                    "pickup": loc["pickup"],
                }
                for loc in world.get("locations", [])
            ],
        }

    def delete(self, owner_id: str, seed_id: str) -> bool:
        seed = self.get(owner_id, seed_id)
        if seed is None:
            return False
        if seed["status"] == "pending":
            raise SeedBusy
        with self.db.connect() as conn:
            conn.execute("DELETE FROM seeds WHERE id = ? AND owner_id = ?", (seed["id"], owner_id))
        shutil.rmtree(self._dir(owner_id, seed["id"]), ignore_errors=True)
        return True

    def count_purgeable(self, owner_id: str, older_than_days: int | None) -> int:
        """How many seeds a purge with the same scope would delete (for confirmation)."""
        where, params = "owner_id = ? AND status != 'pending'", [owner_id]
        if older_than_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
            where += " AND created_at < ?"
            params.append(cutoff.isoformat(timespec="milliseconds"))
        with self.db.connect() as conn:
            return conn.execute(f"SELECT count(*) FROM seeds WHERE {where}", params).fetchone()[0]  # noqa: S608

    def purge(self, owner_id: str, older_than_days: int | None) -> tuple[int, int]:
        """Delete all finished seeds, or those older than N days.

        Returns (deleted, skipped). Seeds still being generated are skipped.
        """
        where, params = "owner_id = ?", [owner_id]
        if older_than_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
            where += " AND created_at < ?"
            params.append(cutoff.isoformat(timespec="milliseconds"))
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT id, status FROM seeds WHERE {where}",  # noqa: S608
                params,
            ).fetchall()
            doomed = [r["id"] for r in rows if r["status"] != "pending"]
            conn.executemany(
                "DELETE FROM seeds WHERE id = ? AND owner_id = ?", [(i, owner_id) for i in doomed]
            )
        for seed_id in doomed:
            shutil.rmtree(self._dir(owner_id, seed_id), ignore_errors=True)
        return len(doomed), len(rows) - len(doomed)
