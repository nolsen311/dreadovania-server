# Dreadovania Server

A self-hosted, mobile-friendly web server for generating and patching
[Randovania](https://github.com/randovania/randovania) seeds for **Metroid Dread**,
and delivering them to a modded Nintendo Switch.

> **Status:** in use. See the [latest release](https://github.com/nolsen311/dreadovania-server/releases/latest)
> for the container image and what's new. The plan and every decision are in
> [`docs/PLAN.md`](docs/PLAN.md) and [`docs/adr/`](docs/adr/).

## What it does

- **Generate** Dread seeds from your phone: from Randovania's presets, your own
  presets (with a full settings editor), or a permalink. Race seeds have no spoiler.
- **Patch** a seed into a ready-to-install mod, using **game files you provide
  yourself** from your own copy of the game.
- **Install** it: download the mod as a zip, send it to your Switch over FTP, or
  install it from the Switch with the companion app,
  [`dreadovania-nx`](https://github.com/nolsen311/dreadovania-nx), which the
  server also keeps up to date.
- **Play with friends:** invite them to your server. Each account has its own
  seeds, presets, game files and Switches, and you race by sharing a permalink.
- **Sign in** with a password or a passkey, over HTTPS.

## Getting started

1. Run the published image with [`compose.yaml`](compose.yaml), pinned by digest
   from the [latest release](https://github.com/nolsen311/dreadovania-server/releases/latest),
   after verifying it (see [`SECURITY.md`](SECURITY.md)).
2. Open the one-time setup link printed in the server's log, and create your account.
3. Set up HTTPS and a hostname: [`docs/https.md`](docs/https.md).
4. Add your game files: [`docs/game-files.md`](docs/game-files.md).
5. Pair the Switch app from **Account**, and invite friends:
   [`docs/playing-with-friends.md`](docs/playing-with-friends.md).

## How this project is built

This project is written by [Claude](https://claude.ai/code), Anthropic's AI
coding assistant, under the oversight of its maintainer
([@nolsen311](https://github.com/nolsen311)). Every change goes through a pull
request that the maintainer reviews, signs off under the
[Developer Certificate of Origin](CONTRIBUTING.md#developer-certificate-of-origin),
and merges. Claude never merges, never tags releases, and never signs off on
anyone's behalf. See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CLAUDE.md`](CLAUDE.md).

## What this project does *not* do

This repository and its container images contain **no game content, keys, or
copyrighted assets**, and never will. To patch a seed you must supply files
extracted from your own legally obtained copy of the game. See the content
policy in [`docs/PLAN.md`](docs/PLAN.md#3-user-supplied-content-and-the-legal-line).

Dreadovania is an unofficial fan project. It is not affiliated with or endorsed
by Nintendo or the Randovania team. All trademarks belong to their owners.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run python scripts/export_openapi.py   # after changing the API
DREADOVANIA_DATA_DIR=./data uv run python -m dreadovania   # serve on http://localhost:8080
```

To build and run the container:

```bash
docker build -t dreadovania:dev .
docker run --rm -p 8080:8080 --read-only --cap-drop ALL -v dreadovania-data:/data dreadovania:dev
```

[`compose.yaml`](compose.yaml) shows a full hardened deployment of the published image.
See [`SECURITY.md`](SECURITY.md) for reporting vulnerabilities and verifying release images.

## License

[GPL-3.0-or-later](LICENSE), compatible with Randovania.
