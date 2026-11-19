# SPDX-License-Identifier: GPL-3.0-or-later
# Samus's materials: which parameters and textures each one uses. Names only, no values.
import os
from collections import Counter
from pathlib import Path

from mercury_engine_data_structures import dread_data
from mercury_engine_data_structures.file_tree_editor import FileTreeEditor
from mercury_engine_data_structures.formats.bsmat import Bsmat
from mercury_engine_data_structures.game_check import Game
from mercury_engine_data_structures.romfs import ExtractedRomFs

root = Path(os.environ.get("DREADOVANIA_ROMFS_DIR") or "/data/romfs")
editor = FileTreeEditor(ExtractedRomFs(root), Game.DREAD)
names = sorted(
    n
    for n in (dread_data.name_for_asset_id(a) for a in editor.all_asset_ids())
    if n and n.startswith("actors/characters/samus/models/") and n.endswith(".bsmat")
)
print("Samus materials:", len(names))
uniforms = Counter()
for name in names:
    try:
        mat = editor.get_parsed_asset(name, type_hint=Bsmat)
    except Exception as e:
        print(f"  {name}: couldn't parse ({type(e).__name__})")
        continue
    stage = mat.raw.shader_stages[0] if mat.raw.shader_stages else None
    u = [p.name for p in stage.uniform_params] if stage else []
    s = [p.filepath.rsplit("/", 1)[-1] for p in stage.sampler_params] if stage else []
    uniforms.update(u)
    print(f"  {name.rsplit('/', 1)[-1]}: shader {mat.raw.shader_path.rsplit('/', 1)[-1]}")
    print(f"    uniforms: {', '.join(u)}")
    print(f"    textures: {', '.join(s)}")
print("uniform names across all Samus materials:")
for n, c in uniforms.most_common():
    print(f"  {n}: {c}")
