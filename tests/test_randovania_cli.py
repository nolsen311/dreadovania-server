# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 0 findings, kept as regression tests (ADR 0010).

These run the real Randovania CLI headless, exactly as the server will:
generate a Dread seed from a bundled preset, then take the export path as far
as it can go without user-supplied game files. No game content is involved.
"""

import re
import subprocess
import sys
from importlib.metadata import requires, version
from pathlib import Path

import pytest
import randovania
from packaging.requirements import Requirement
from packaging.version import Version

PRESET = Path(randovania.__file__).parent / "games/dread/presets/starter_preset.rdvpreset"


def _randovania(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "randovania", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_dread_patcher_pin_matches_randovania() -> None:
    """open-dread-rando must be the exact version Randovania's exporters extra pins.

    The two are pinned separately in pyproject.toml (we skip the extra, which
    would pull in every game's patcher), so an independent bump would drift.
    """
    pins = [
        Requirement(r)
        for r in requires("randovania") or []
        if r.startswith("open-dread-rando") and "exporters" in r
    ]
    assert len(pins) == 1, pins
    assert pins[0].specifier.contains(version("open-dread-rando")), (
        f"randovania {version('randovania')} wants {pins[0].specifier}, "
        f"installed open-dread-rando is {version('open-dread-rando')}"
    )


def test_cryptography_override_still_needed() -> None:
    """pyproject.toml overrides Randovania's cryptography<49 pin (ADR 0010).

    Fails when the override stops being active, or when Randovania itself
    allows a fixed release, which means the override should be removed.
    """
    assert Version(version("cryptography")) >= Version("50"), "override not active"
    pins = [Requirement(r) for r in requires("randovania") or [] if r.startswith("cryptography")]
    assert len(pins) == 1, pins
    assert not pins[0].specifier.contains("50.0.0"), (
        f"randovania {version('randovania')} now allows {pins[0].specifier}: remove "
        "override-dependencies from pyproject.toml and update ADR 0010"
    )


def test_gui_is_not_installed() -> None:
    """The headless server must not pull in Qt (PySide6 is Randovania's 'gui' extra)."""
    with pytest.raises(ModuleNotFoundError):
        __import__("PySide6")


@pytest.fixture(scope="module")
def generated_seed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    out = tmp_path_factory.mktemp("seed") / "seed.rdvgame"
    result = _randovania(
        "layout", "generate-from-presets", "--file", PRESET, "--seed-number", "12345", out
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.is_file()
    return out, result.stdout


def test_generate_dread_seed_headless(generated_seed: tuple[Path, str]) -> None:
    seed, stdout = generated_seed
    assert re.search(r"^Permalink: \S+$", stdout, re.MULTILINE), stdout
    assert re.search(r"Hash: \w+", stdout), stdout

    described = _randovania("layout", "describe", seed)
    assert described.returncode == 0, described.stderr
    assert "Metroid Dread" in described.stdout


def test_export_runs_until_game_files_are_needed(
    generated_seed: tuple[Path, str], tmp_path: Path
) -> None:
    """The whole export pipeline is reachable from the CLI.

    With an empty input directory, export must get past patcher-data and into
    the Dread patcher, stopping only when it looks for the user's RomFS.
    """
    seed, _ = generated_seed
    patcher_data = tmp_path / "patcher.json"
    data = _randovania("layout", "patcher-data", "--layout-file", seed, "--output", patcher_data)
    assert data.returncode == 0, data.stderr
    assert patcher_data.stat().st_size > 0

    (tmp_path / "input").mkdir()
    (tmp_path / "output").mkdir()
    export = _randovania(
        "layout", "export-game", "--patcher-data", patcher_data, "dread",
        "--input-path", tmp_path / "input", "--output-path", tmp_path / "output",
        "--target-platform", "atmosphere",
    )  # fmt: skip
    assert export.returncode != 0
    assert "system/files.toc" in export.stderr, export.stderr
