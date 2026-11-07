# Dreadovania Server

A self-hosted, mobile-friendly web server for generating and patching
[Randovania](https://github.com/randovania/randovania) seeds for **Metroid Dread**,
and delivering them to a modded Nintendo Switch.

> **Status:** planning. Nothing here is runnable yet. See [`docs/PLAN.md`](docs/PLAN.md).

## What it will do

- Generate Dread seeds from presets or permalinks, from your phone.
- Patch seeds into a ready-to-install mod using **game files you provide yourself**.
- Download the mod as a zip, or push it straight to your Switch over FTP.
- Serve an API for the companion Switch app,
  [`dreadovania-nx`](https://github.com/nolsen311/dreadovania-nx).

## What this project does *not* do

This repository and its container images contain **no game content, keys, or
copyrighted assets**, and never will. To patch a seed you must supply files
extracted from your own legally obtained copy of the game. See the content
policy in [`docs/PLAN.md`](docs/PLAN.md#3-user-supplied-content-and-the-legal-line).

Dreadovania is an unofficial fan project. It is not affiliated with or endorsed
by Nintendo or the Randovania team. All trademarks belong to their owners.

## License

[GPL-3.0](LICENSE), matching Randovania.
