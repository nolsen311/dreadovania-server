# 0025. Randovania's Dread cosmetic options, including separate save slots

- Status: Proposed
- Date: 2026-11-12
- Takes up: [ADR 0016](0016-phase3-send-to-switch.md), "Later" (cosmetic options)

## Context

Randovania keeps **cosmetic options** apart from presets. They change how a
patched seed looks and behaves, but not where items are. So they're not in the
permalink or the seed hash, and two racers can use different ones on the same
seed. Every seed is currently patched with Randovania's defaults.

Randovania 12.0's `DreadCosmeticPatches`
(`randovania/games/dread/layout/dread_cosmetic_patches.py`) has these options:

| Option | Type | Randovania's default |
|---|---|---|
| `show_boss_lifebar`, `show_enemy_life`, `show_enemy_damage`, `show_player_damage` | on/off | off |
| `show_dna_in_hud`, `show_death_counter` | on/off | off |
| `show_room_names` | never / always / when entering a room | never |
| `missile_cosmetic` | vanilla, or one of seven pride-flag colour sets for missile tanks | vanilla |
| `music_volume`, `sfx_volume`, `ambience_volume` | 0–100 | 100 |
| `alt_ice_missile`, `alt_storm_missile`, `alt_diffusion_beam`, `alt_bomb`, `alt_cross_bomb`, `alt_power_bomb`, `alt_closed` | default / alternate shield texture | default (power bomb: alternate) |
| `enable_auto_tracker` | on/off (lets a tracker on the network read the game) | on |
| `enable_debug_logging` | on/off (Lua logs, in supported emulators) | off |
| `separate_save_slots` | on/off | off |

**Separate save slots** is the one that matters most for Dreadovania, whose job
is swapping seeds. Here is what the installed patcher (`open_dread_rando`'s
`patch_saveslot`) does with it. It writes a key,
`RDV_<the seed's shareable hash>_<world UUID>`, into the mod, and turns the
save-slot borders dark green. The game then keeps that seed's three slots in a
folder of their own inside Dread's save data. Randovania's own description says
each slot takes about 300 KB, and is freed by deleting the game's save data in
System Settings. The world UUID is fixed for a single-player seed, and the hash
comes from the seed itself, so **patching the same seed again keeps its saves**.
Without the option, every seed shares the same three slots, and loading one
seed's save in another seed mixes the two.

**The CLI isn't enough.** `randovania layout patcher-data` always uses
`cosmetic_patches.default()` and has no option for others. The patcher data
can't simply be edited afterwards either. Missile colours are picked per pickup
with the seed's random generator, and room names add lookup tables, so the
result depends on Randovania's own code.

## Decision

1. **Per account.** Each account has its own cosmetic options, set on a new
   **Cosmetics** page (linked from the Account page). They apply to every seed
   that account patches, from the web UI, the API or the Switch app. They aren't
   part of a preset, because they don't change the seed.
2. **Every option is offered except `enable_debug_logging`.** It only does
   anything in emulators, and Dreadovania installs to a Switch. It stays off.
3. **Separate save slots are on by default** for Dreadovania accounts. This is
   the one place the defaults differ from Randovania's. The Cosmetics page
   explains what it costs (about 300 KB per slot used, freed only by deleting
   Dread's save data) and lets the account turn it off. Every other default is
   Randovania's.
4. **The adapter module** (ADR 0002) is `dreadovania.randovania_adapter`. Its one
   command, `patcher-data`, does what Randovania's command of that name does, but
   with the given cosmetic options. It still runs as a subprocess, the way the
   CLI does, with the same priority, timeout and process group. `export-game`
   stays a plain CLI call.
5. **The server validates the options itself** (types, enum values, 0–100
   volumes) before they reach Randovania. A test checks that the server's list
   matches `DreadCosmeticPatches` exactly: every field, type, default and enum
   value. So a Randovania upgrade that adds or changes an option fails the tests,
   instead of being silently left out.
6. **Each patched seed records the options it was patched with.** When the
   account's options have changed since, the seed page says so and offers
   **Patch again**. The API shows the recorded options on the seed's mod, and
   whether they match the account's current ones. Patching again doesn't create a
   new seed: with separate save slots, its saves carry over.
7. **API.** `GET`/`PUT /api/v1/cosmetics` reads and replaces the account's
   options. This is an addition to `/api/v1`, not a breaking change.

## Consequences

- An upgrade of Randovania now also means checking the adapter against
  `randovania/cli/commands/patcher_data.py`. The tests run it end to end on a
  real generated seed (no game files needed), and the field check above catches
  new options.
- Turning separate save slots on or off only affects seeds patched afterwards.
  The seed page's **Patch again** note covers seeds patched before.
- With separate save slots on, Dread's save data slowly fills with
  old seeds' folders. Clearing them from the console deletes every Dread save.

### Later (not part of this ADR)

- **Save data per seed on the Switch** (`dreadovania-nx`). With separate save
  slots, the game already keeps each seed's saves apart. What's left for the app
  is backup and clean-up. It could open Dread's save data (as save managers like
  JKSV do), copy one seed's `RDV_…` folder to the SD card or the server, and
  delete the folders of seeds that are gone, without touching other saves. This
  needs its own ADR in that repository. Writing to a game's save data is riskier
  than anything the app does today.
- A per-seed override of the account's options, if someone wants it.
