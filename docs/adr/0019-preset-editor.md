# 0019. Editing every randomizer setting in the web UI

- Status: Proposed
- Date: 2026-11-10

## Context

Seeds come from presets: Randovania's bundled Dread presets, or a `.rdvpreset`
file imported from the desktop app. Changing a setting today means owning a PC
with Randovania installed. The goal is to edit **every** Dread setting from a
phone, in the server's web UI.

Randovania 12.0 defines the settings as `DreadConfiguration`: about 35 fields,
several of them large structures (the pickup pool, trick levels, excluded
locations, door types). Its desktop app groups them into 13 tabs. Preset files
carry a schema version, and Randovania migrates old files when it loads them.

## Decision

1. **Bundled presets stay read-only.** *Customize* copies a preset (bundled or
   imported) into a new preset of the user's, named "Copy of …", with a new
   UUID, as Randovania's own Customize does (Randovania 12 no longer records
   which preset a copy came from). Only the user's own presets are edited.
2. **One adapter module**, `rdv_presets.py`, holds everything that knows
   Randovania's preset internals (ADR 0002): loading and migrating a preset,
   the editable fields with their labels and options, applying changes, and
   checking the result. Nothing else imports Randovania's preset classes.
3. **Changes are checked by Randovania itself.** After every save the preset is
   loaded through Randovania's `VersionedPreset`/`Preset` and packed into a
   permalink, which applies the same bounds the desktop app does. A change that
   fails is refused with Randovania's reason, and the stored preset is untouched.
   Presets are stored in Randovania's current schema.
4. **Complete by test.** A test lists every `DreadConfiguration` field and
   requires each one to be editable, or listed as "not editable yet" with a
   reason. A setting Randovania's own app doesn't offer either (a generator
   detail) may be listed as "not offered", also with a reason. A Randovania upgrade that adds a setting then fails CI until it is
   handled, so "every setting" stays true.
5. **The same tabs as the desktop app, in its order:** Logic (trick levels),
   Generation, Hints, Locations, Goal, Pickup pool, Transporters, Starting
   area, Doors, Energy, Other, Lights, Chaos. Labels and help text follow
   Randovania's own wording (GPL-3.0, like this project; credited in the
   module), so its guides and community advice still apply. Each tab is a
   server-rendered form, saved with htmx, usable on a phone.
6. **Small API.** `POST /presets/{id}/copy`, `PUT /presets/{id}` (a whole
   `.rdvpreset`, checked as above) and `GET /presets/{id}/rdvpreset` (export,
   to open in the desktop app). Device tokens gain no new routes: the Switch
   app picks presets, it doesn't edit them.
7. **Warnings, not walls.** Randovania's own "dangerous settings" and
   "unsupported features" checks are shown on the preset and before
   generating, as the desktop app does. They don't block saving.

## Pull requests

1. This ADR.
2. The adapter: copy, rename and describe, export, `PUT`, the checks and the
   coverage test; the editor page with the simple tabs (Generation, Goal,
   Energy, Other, Lights, Chaos).
3. Pickup pool (standard and ammo pickups).
4. Logic (every trick's level, minimal logic, dangerous logic) and Hints.
5. Locations (randomization mode, excluded locations) and Starting area.
6. Doors and Transporters.

After pull request 6 the coverage test's "not editable yet" list is empty.
Settings Randovania's app doesn't show for Dread but that matter to players
(such as warp to start) are still offered, in plain wording of our own.

## Consequences

- Upgrading Randovania means checking the adapter and its coverage test; the
  rest of the server doesn't change.
- A preset edited here opens in the desktop app (export), and one from the
  desktop app can be edited here (import, then edit).
- Permalinks and `.rdvgame` files stay tied to the Randovania version, as now.
