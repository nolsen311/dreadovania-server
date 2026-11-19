# Research: recolouring Samus's suit ourselves (parked)

Status: **parked** on the `claude/suit-colour-research` branch, 2026-11-19. Nothing
here is part of the server. To pick it up again, start with "Next step" below.

## The question

Instead of layering another person's suit-colour mod (ADRs 0026 and 0027), could the
server recolour Samus's suit itself, from each player's own game files, when a seed
is patched? The randomizer already does this for missile tanks and doors
(`open-dread-rando`'s `cosmetic_patches/missile_color_patcher.py` and
`door_locks/custom_door_types.py`): it edits a material's colour parameters and
ships the material as a small file the seed's loader swaps in.

Only the game's file formats are studied here, through
`mercury_engine_data_structures`. No mod's files were opened or used.

## What's known (measured on the maintainer's game files, version 2.1.0)

- **Samus's suit textures are loose files** under `romfs/textures/actors/characters/samus/`
  (83 of them). `samus.pkg` holds her models and materials (5,685 assets, no
  textures). See ADR 0027's Context.
- **67 Samus materials.** Every suit part uses the shader `mp_samuspbr`: the body
  and head of each suit (Power, Varia, Gravity, Hyper), the arm cannons, Gravity's
  cutscene hand, and the Speed Booster form (`sburst`). 19 materials in all.
- **Their parameters** (`scripts/research/samus_materials.py`):
  - `vAlbedoEmissiveColor` and `fAlbedoEmissiveFactor`: a glow colour and its
    strength. The colour is green on Power, Varia and Gravity, and black on Hyper.
    The strength is 3 to 50, depending on the material.
  - `vConstant0`–`vConstant5`: mostly zero. `vConstant0` is `(0, 0, 0, 1)` and
    `vConstant4` is `(1, 0, 0, 0)` on every suit body. Hyper also has a non-zero
    `vConstant3`. None of these looks like a colour multiplier, which would be
    `(1, 1, 1, 1)`.
  - Textures: `texBaseColor` (`_bc`, the suit's paint), `texAttributes` (`_at`),
    `texNormals` (`_nm`) and an empty `texCubemap`.
  - The arm cannons have no glow colour (`vAlbedoEmissiveColor` is zero), so the
    cannon's green comes from somewhere else.
- **How the seed's loader takes a changed asset:** the file at its own path under
  `romfs/`, its path in `replacements.json` (`{"replacements": [paths]}`), and its
  size in `system/files.toc` (`mercury_engine_data_structures`'
  `FileTreeEditor.save_modifications`).

## What was tried, on hardware (`scripts/research/suit_glow_test.py`)

All runs patched a copy of one seed. The script reported every change it wrote.

| Run | Changed | Result on the Switch |
|---|---|---|
| Glow colour `1 0 1` (magenta) | `vAlbedoEmissiveColor` on the 13 materials with a glow | No visible change. Power and Varia glow stayed green. |
| Tint `0.6 0 0.6` (`--tint`) | `vConstant0` on all 19 suit materials | No visible change. |
| Glow strength `--factor=500` | `fAlbedoEmissiveFactor` on all 19 | **Not yet seen.** Copying to the SD card kept corrupting; parked before it was checked. |

## Next step

Install the `--factor=500` copy (or `--factor=0`, lights off, which is easier to
spot) and look at the lights on her body and helmet in the Power Suit:

- **The lights change:** the game loads edited materials, and sets the glow
  *colour* itself at runtime. Look for the parameter or texture it uses instead.
- **No change at all:** edited materials in `samus.pkg` aren't used. Try the
  randomizer's own method instead: new material files with new names, plus copies
  of the models that point to them (`model_patcher.py` and `material_patcher.py` in
  `open-dread-rando`).

If materials can't carry the suit's main colour, the remaining route is recolouring
the `_bc` textures. That means decompressing, unswizzling (Tegra), BCn-decoding, a
masked recolour, then re-encoding: a new dependency and a much bigger job.

## The scripts

Run them on the server, inside the container. They print only names, counts and
sizes, except `suit_glow_test.py`, which also prints the parameters it changes.

```bash
docker exec -i <container> python - < scripts/research/samus_packs.py
docker exec -i <container> python - < scripts/research/samus_materials.py
docker exec -i <container> python - SEED_ID 1 0 1 < scripts/research/suit_glow_test.py
docker exec -i <container> python - SEED_ID --factor=0 < scripts/research/suit_glow_test.py
```

`suit_glow_test.py` writes to `<data>/suit-test/<seed>/mod`. Copy that out with
`docker cp`. Replace `atmosphere/contents/010093801237c000` on the SD card with its
`contents/010093801237c000`, then check the copy with checksums after remounting.
To go back, install the seed normally.
