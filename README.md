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
uv run python -m dreadovania              # serve on http://localhost:8080
```

To build and run the container:

```bash
docker build -t dreadovania:dev .
docker run --rm -p 8080:8080 --read-only --cap-drop ALL dreadovania:dev
```

[`compose.yaml`](compose.yaml) shows a full hardened deployment of the published image.
See [`SECURITY.md`](SECURITY.md) for reporting vulnerabilities and verifying release images.

## License

[GPL-3.0-or-later](LICENSE), compatible with Randovania.
