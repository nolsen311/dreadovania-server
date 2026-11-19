# SPDX-License-Identifier: GPL-3.0-or-later
# Which packs carry Samus's assets? Prints pack names and counts only.
import os
import sys
from collections import Counter
from pathlib import Path

from mercury_engine_data_structures import dread_data
from mercury_engine_data_structures.file_tree_editor import FileTreeEditor
from mercury_engine_data_structures.game_check import Game
from mercury_engine_data_structures.romfs import ExtractedRomFs

root = Path(
    sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DREADOVANIA_ROMFS_DIR") or "/data/romfs"
)
editor = FileTreeEditor(ExtractedRomFs(root), Game.DREAD)

samus = {}
for asset_id in editor.all_asset_ids():
    name = dread_data.name_for_asset_id(asset_id)
    if name and name.startswith("actors/characters/samus/"):
        samus[asset_id] = name

textures = Counter()
everything = Counter()
for asset_id, name in samus.items():
    for pkg in editor.find_pkgs(asset_id):
        everything[pkg] += 1
        if name.endswith(".bctex"):
            textures[pkg] += 1

print(f"Samus assets: {len(samus)} ({sum(n.endswith('.bctex') for n in samus.values())} textures)")
print("packs with Samus's textures (pack: textures / all Samus assets):")
for pkg, n in sorted(textures.items(), key=lambda kv: -kv[1]):
    print(f"  {pkg}: {n} / {everything[pkg]}")
other = sorted(set(everything) - set(textures))
print(f"packs with other Samus assets but no textures: {len(other)}")
for pkg in other[:20]:
    print(f"  {pkg}: {everything[pkg]}")
body = "actors/characters/samus/models/textures/powerbody_bc.bctex"
if editor.does_asset_exists(body):
    print(f"{body} is in: {', '.join(editor.find_pkgs(body)) or '(no pack: loose)'}")
else:
    print(f"{body}: not found")
