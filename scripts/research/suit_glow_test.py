# SPDX-License-Identifier: GPL-3.0-or-later
# Prototype, not part of the server: recolour Samus's suit glow in a COPY of one
# patched seed, from your own game files. The seed itself is left untouched.
#
#   python - SEED_ID R G B [--tint]      (colours from 0 to 1, e.g. 1 0 1 for magenta)
#   python - SEED_ID --factor=N          (the glow's strength only: 0 = off, 500 = glaring)
#
# Default: changes vAlbedoEmissiveColor (the suit's glow, green in the game) on every
# suit material that has a glow. --tint instead sets the colour into vConstant0, an
# experiment to see whether that parameter tints the whole suit.
# Writes to <data>/suit-test/<seed>/ and prints only paths and counts.
import json
import os
import shutil
import sys
from pathlib import Path

from mercury_engine_data_structures import dread_data
from mercury_engine_data_structures.file_tree_editor import FileTreeEditor
from mercury_engine_data_structures.formats.bsmat import Bsmat
from mercury_engine_data_structures.formats.toc import Toc
from mercury_engine_data_structures.game_check import Game
from mercury_engine_data_structures.romfs import ExtractedRomFs

args = [a for a in sys.argv[1:] if not a.startswith("--")]
tint = "--tint" in sys.argv
factor = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--factor=")), None)
if not ((factor is None and len(args) == 4) or (factor is not None and len(args) == 1)):
    sys.exit("usage: python - SEED_ID R G B [--tint]   or   python - SEED_ID --factor=N")
seed_id = args[0]
rgb = [float(x) for x in args[1:]] if factor is None else None

data = Path(os.environ.get("DREADOVANIA_DATA_DIR") or "/data")
game = Path(os.environ.get("DREADOVANIA_ROMFS_DIR") or data / "romfs")
found = list((data / "seeds").glob(f"*/{seed_id}/mod"))
if len(found) != 1:
    sys.exit(f"patched seed {seed_id} not found under {data / 'seeds'}")
out = data / "suit-test" / seed_id
if out.exists():
    shutil.rmtree(out)
shutil.copytree(found[0], out / "mod")
romfs = out / "mod" / "contents" / "010093801237c000" / "romfs"

editor = FileTreeEditor(ExtractedRomFs(game), Game.DREAD)
toc = Toc.parse((romfs / "system/files.toc").read_bytes(), Game.DREAD)
replacements_file = romfs / "replacements.json"
replacements = json.loads(replacements_file.read_text("utf-8"))["replacements"]

changed = 0
for asset_id in list(editor.all_asset_ids()):
    name = dread_data.name_for_asset_id(asset_id)
    if not name or not name.endswith(".bsmat"):
        continue
    if not name.startswith("actors/characters/samus/models/"):
        continue
    seed_copy = romfs / name  # the seed's own version wins, if it changed this material
    raw = seed_copy.read_bytes() if seed_copy.is_file() else editor.get_raw_asset(asset_id)
    mat = Bsmat.parse(raw, Game.DREAD)
    if not mat.raw.shader_path.endswith("mp_samuspbr.bshdat"):
        continue
    params = {p.name: p for p in mat.raw.shader_stages[0].uniform_params}
    if factor is not None:
        target = params.get("fAlbedoEmissiveFactor")
        if target is None:
            continue
        before = list(target.value)
        target.value = [factor]
        print("  strength", before, "->", target.value)
        target = None
    elif tint:
        target = params.get("vConstant0")
    else:
        target = params.get("vAlbedoEmissiveColor")
        if target is not None and not any(target.value[:3]):
            target = None  # no glow on this material (the Hyper suit): leave it
    if factor is None:
        if target is None:
            continue
        target.value = [*rgb, target.value[3]]
    built = mat.build()
    seed_copy.parent.mkdir(parents=True, exist_ok=True)
    seed_copy.write_bytes(built)
    toc.add_file(asset_id, len(built))
    if name not in replacements:
        replacements.append(name)
    changed += 1
    print("changed", name.rsplit("/", 1)[-1])

toc.add_file(Toc.system_files_name(), len(toc.build()))
(romfs / "system/files.toc").write_bytes(toc.build())
replacements_file.write_text(json.dumps({"replacements": replacements}, indent=4), "utf-8")
print(f"{changed} materials changed; the copy is in {out / 'mod'}")
