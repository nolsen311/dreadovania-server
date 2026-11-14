# 0027. Extra mods, measured: what a suit-colour mod replaces, and a "suit only" choice

- Status: Proposed
- Date: 2026-11-13
- Supersedes: [ADR 0026](0026-extra-mods.md)'s Context (its description of the suit
  mod and uncertainty 1), and decisions 5 and 6. ADR 0026's other decisions stand.

## Context

ADR 0026 described a suit-colour mod from its file list alone, and left open
whether the game checks file sizes in `system/files.toc`. The maintainer then
measured their own game files (version 2.1.0) with `mercury_engine_data_structures`,
printing only names, counts and sizes. The mod itself was never downloaded.

**Where Samus's suit lives in the game:**

- **Her textures are loose files**, under `romfs/textures/actors/characters/samus/`
  (83 of them). No main-game pack contains them; only the boss-rush packs
  (`packs/maps/s2xx_bossrush_*`) carry copies.
- **`packs/players/samus.pkg`** holds her models and materials, 5,685 assets and
  no textures.
- **The cutscene close-up of her head and helmet**
  (`actors/characters/samus_facial/models/cut_samusfacial.bcmdl` and its helmet,
  visor and face materials) is in exactly four area packs: `s010_cave` (Artaria),
  `s070_basesanc` (Ferenia), `s080_shipyard` (Hanubia) and `s090_skybase`
  (Itorash). Only Itorash also has the Hyper Beam version
  (`cut_samushyperfacial`).

The suit mod replaces the loose textures, `samus.pkg` and exactly those four area
packs. So its parts line up with the game's:

| Part of the mod | What it changes | Size |
|---|---|---|
| Loose textures | The suit's colour in gameplay | small |
| `samus.pkg` | Her models and materials, most likely the tint and glow | about 180 MB |
| Four area packs | The helmet in cutscene close-ups | about 430 MB, whole copies of four areas |

This explains the mod's structure from the game's files, but only the mod's own
files can confirm it.

**`files.toc` (ADR 0026, uncertainty 1):** the game's `files.toc` lists 117,860
files. Every pack the mod replaces is listed with exactly its size on disk. Loose
textures aren't listed. So a replaced pack needs its real size in the delivered
`files.toc`, and a loose texture needs nothing.

**The version risk (ADR 0026, uncertainty 2) is concentrated in the area packs.**
Each is a whole copy of an area from the mod author's game. `samus.pkg` is also a
whole copy, but of Samus only, and it's loaded everywhere, so a problem shows at
once.

## Decision

1. **Each extra mod has parts, and the account picks which apply.** When a mod is
   uploaded, the server sorts its files into:
   - **Textures:** loose files outside `packs/`.
   - **Samus:** `packs/players/…`.
   - **Areas:** `packs/maps/…`.
   - **Other packs:** anything else under `packs/`, refused unless ADR 0026
     decision 2 allows it.

   The mod's page offers **Suit only** (textures and Samus) or **Suit and
   cutscene helmet** (all three), with each part's size. **Suit only** is the
   default whenever the mod replaces area packs. The page says what the area packs
   add, and that they must match the game version of the account's game files.
   This replaces ADR 0026 decision 6.
2. **`files.toc` gets real sizes for packs only.** For each pack a delivery
   replaces, the delivered `system/files.toc` has that pack's size. Loose files
   aren't added to it, matching the game's own `files.toc`. If a replaced pack isn't
   listed in the seed's `files.toc`, the mod's page says so, and that pack is left
   out of the delivery. This replaces ADR 0026 decision 5.
3. **No repacking.** The server copies the mod's files whole, as the mod's own
   instructions do. It never extracts assets from them or builds new packs. The
   mod's licence allows use but not modification. A slimmer "helmet only" option
   would need rebuilt area packs, so it isn't offered.

## Consequences

- With **Suit only**, a delivery grows by about 180 MB plus the textures, rather
  than about 700 MB. It no longer carries whole copies of four areas, so the
  version risk drops to `samus.pkg`.
- With **Suit only**, Samus's helmet keeps its usual colour in cutscene close-ups in
  Artaria, Ferenia, Hanubia and Itorash.
- ADR 0026's hardware checklist still applies, for both choices. It must also
  cover a cutscene with the helmet close-up in one of those four areas.
- Other mods may be built differently. The parts and the size rules apply to any
  extra mod. The page shows each part's files, so an unusual mod is visible before
  it's used.
