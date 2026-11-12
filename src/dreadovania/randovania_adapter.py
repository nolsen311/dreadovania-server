# SPDX-License-Identifier: GPL-3.0-or-later
"""The one place the server calls Randovania's code rather than its CLI (ADR 0002).

Randovania's ``layout patcher-data`` always uses the default cosmetic options. The
``patcher-data`` command here does the same as that command
(``randovania/cli/commands/patcher_data.py`` in Randovania 12.0), with the
cosmetic options given in a JSON file (ADR 0025). It runs as a subprocess, like
the CLI::

    python -m dreadovania.randovania_adapter patcher-data \\
        --layout-file seed.rdvgame --cosmetics cosmetics.json --output patcher-data.json

Check it against that file whenever Randovania is upgraded.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def patcher_data(layout_file: Path, cosmetics_file: Path, output: Path) -> None:
    import randovania
    from randovania.interface_common.worlds_configuration import WorldsConfiguration
    from randovania.layout.layout_description import LayoutDescription

    randovania.setup_logging("INFO", None, quiet=True)  # as `python -m randovania` does
    layout = LayoutDescription.from_file(layout_file)
    worlds = WorldsConfiguration(0, {i: f"Player {i + 1}" for i in range(layout.world_count)})
    preset = layout.get_preset(worlds.world_index)
    patches_type = preset.game.data.layout.cosmetic_patches
    cosmetics = patches_type.from_json(json.loads(cosmetics_file.read_text()))
    factory = preset.game.patch_data_factory(layout, worlds, cosmetics)
    output.write_text(json.dumps(factory.create_data(), indent=4))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m dreadovania.randovania_adapter")
    commands = parser.add_subparsers(dest="command", required=True)
    data = commands.add_parser("patcher-data", help="Write the patcher data for a seed.")
    data.add_argument("--layout-file", type=Path, required=True)
    data.add_argument("--cosmetics", type=Path, required=True)
    data.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    patcher_data(args.layout_file, args.cosmetics, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
