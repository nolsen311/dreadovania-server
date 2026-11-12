# 0026. Extra mods layered onto each seed

- Status: Proposed
- Date: 2026-11-12
- Takes up: [ADR 0016](0016-phase3-send-to-switch.md), "Later" (extra mods), and
  `docs/PLAN.md`'s "Mods alongside seeds"

## Context

Randovania's cosmetic options (ADR 0025) don't include Samus's suit colours.
Those come from other people's mods, which replace game files. Each player
downloads them themselves, and the server must never hold one that isn't the
player's own copy. The content policy rules out adding any of them to the
project, or linking to them.

We looked at one real suit-colour mod's file list, without downloading it, to see
what such mods look like. It ships two kinds of folder, and you copy one colour's
folder over the base folder:

- **Whole replacement packs** (`romfs/packs/players/samus.pkg`, and four area
  packs under `romfs/packs/maps/`), 36–184 MB each.
- **Loose textures** (`romfs/textures/actors/characters/…/*.bctex`).

That mod is about 1.35 GB for every colour together, and about 700 MB once
assembled. Its licence lets you use it but not modify or redistribute it.

A patched seed (exlaunch mode, ADR 0013) writes something different. The patcher
is `mercury_engine_data_structures`' `save_modifications` with
`OutputFormat.ROMFS`, and it writes only these:

- the changed assets as loose files;
- `system/files.toc`, the game's list of files and their sizes;
- `replacements.json`, which tells the seed's exefs loader which assets inside
  packs the loose files replace;
- `packs/system/system.pkg`.

It writes no other pack. The loader reads the loose files before a pack's own
contents, so the seed's changes still apply inside another mod's replacement
packs.

So the two sets of files don't overlap, and layering is mostly copying. Three
things are uncertain:

1. **`files.toc`.** The seed writes its own copy. If it lists pack sizes and the
   game checks them, a replacement pack with a different size could fail to load.
   The suit mod ships no `files.toc`, and its colour variants differ in size by a
   few bytes, which suggests the game doesn't check. That isn't confirmed: the
   check below answers it on real game files.
2. **Game version.** A whole area pack is a copy of the mod author's game, at one
   version. On another version (1.0.0 against 2.1.0, ADR 0013), those areas may
   break.
3. **Separate save slots** (ADR 0025) change `gui/scripts/mainmenucomposition.bmscp`.
   It's a loose file the suit mod doesn't touch. Another mod could.

## Decision

1. **A private library per account.** Under **Account → Extra mods**, an account
   uploads a mod as a zip (with the same streaming upload as game files, ADR 0013).
   The server stores it in `data/accounts/<account>/extra-mods/<id>/romfs/`.
   - **Layouts accepted:** `romfs/…`, `atmosphere/contents/010093801237c000/romfs/…`,
     or folders that each contain one of those.
   - **Mods in several folders:** when a zip has several folders, the account picks
     the ones to combine and their order. For the suit mod, that's the base folder
     and then one colour. Only the combined result is kept, so 700 MB rather than
     1.35 GB.
   - **Size limit:** a per-account limit (`DREADOVANIA_MAX_EXTRA_MOD_BYTES`, 4 GiB by
     default) keeps a server's disk in check.
2. **What an extra mod may contain:** files under `romfs/` only.
   - **Refused:** `exefs/` (the seed's own loader lives there), `system/files.toc`,
     `replacements.json` and `packs/system/system.pkg`.
   - **Also refused:** links, and paths that leave the folder.
   - **Said why:** the upload page names each refused file and the reason.
3. **Turned on per account, in order.** The account ticks which extra mods apply.
   When two mods ship the same file, the later one wins, and the page says which
   files that affects. Extra mods don't change the seed, so permalinks, races and
   spoilers are unaffected.
4. **Layered when a seed is delivered, not when it's patched.** The seed's patched
   files stay as Randovania wrote them. The zip download, **Send to Switch** (ADR
   0016) and the Switch app's manifest and tar (ADR 0018) all serve the seed's files
   with the active extra mods' files added.
   - **A seed file always wins.** If an extra mod ships a file the seed also wrote,
     that mod is left out of the delivery, and the seed page says which mod and
     file.
   - **One record of installed files.** The installed-files record (ADR 0016)
     includes the extra mods' files. So replacing a seed, from the server or the
     app, removes them too, and other mods are kept as before.
5. **`files.toc` follows the files.** For every file an extra mod adds or replaces,
   the delivered `system/files.toc` gets that file's real size. The server builds
   it with the same library Randovania uses, `mercury_engine_data_structures`'
   `Toc`. This matches what the patcher itself does for the files it writes, and
   doesn't depend on the answer to Context point 1.
6. **Version warning.** An extra mod that replaces a whole area pack
   (`packs/maps/…`) is flagged on its page. The page says the mod must be made for
   the same game version as the account's game files. The server can't tell a
   pack's version, so it warns rather than refuses.
7. **Private and unchanged.**
   - An extra mod is only ever served inside its owner's own deliveries. It's never
     downloadable alone, never shared between accounts and never logged.
   - The server doesn't modify its files: layering copies them, as the mod's own
     instructions do.
   - Removing an extra mod, or the account, deletes its files.
   - The project's tests use generated files only.
8. **Hashes once.** The Switch app's manifest has a SHA-256 for every file
   (ADR 0018). Each extra mod's files are hashed once, at upload, not on every
   delivery.

## Consequences

- Deliveries get bigger: about 700 MB more for the suit mod. Over FTP or the
  Switch app's Wi-Fi, that's minutes. The page shows each delivery's total size.
- Each delivery needs its own copy of `files.toc`. It's built on the fly from the
  seed's copy and the extra mods' sizes, and is small.
- The Switch app needs no change. Its manifest and tar already list whatever files
  the server sends.
- Context points 1–3 are only settled by trying it on the console. The first
  implementation PR includes a hardware checklist: a seed with the suit mod
  layered, played in each area whose pack the mod replaces, with separate save
  slots on.

## The check for Context point 1

On a server with game files, this prints each relevant file's size in the game's
own `files.toc` next to its size on disk. Nothing from the game is printed but the
sizes.

```bash
docker compose exec -T dreadovania python - <<'EOF'
import os
from pathlib import Path

from mercury_engine_data_structures.formats.toc import Toc
from mercury_engine_data_structures.game_check import Game

root = Path(os.environ.get("DREADOVANIA_ROMFS_DIR") or "/data/romfs")
toc = Toc.parse((root / "system/files.toc").read_bytes(), Game.DREAD)
print("files listed in files.toc:", len(list(toc.get_all_asset_id())))
for path in [
    "packs/players/samus.pkg",
    "packs/maps/s010_cave/s010_cave.pkg",
    "packs/maps/s070_basesanc/s070_basesanc.pkg",
    "packs/maps/s080_shipyard/s080_shipyard.pkg",
    "packs/maps/s090_skybase/s090_skybase.pkg",
    "packs/system/system.pkg",
    "textures/actors/characters/samus/models/textures/powerbody_bc.bctex",
]:
    file = root / path
    actual = file.stat().st_size if file.exists() else None
    print(f"{path}: in files.toc {toc.get_size_for(path)}, on disk {actual}")
EOF
```

If the packs are listed (a number, not `None`), the game knows their sizes, and
decision 5 matters. If they aren't, `files.toc` only covers loose files, and
decision 5 is just a precaution.
