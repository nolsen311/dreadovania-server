# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# Every base image is pinned by multi-arch index digest, so the same line works
# for linux/amd64 and linux/arm64 (ADR 0003). Dependabot updates the digests.
# Both stages must use the same Python image: the virtualenv links to its interpreter.

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# ---- build: resolve the locked dependencies into a virtualenv ----
FROM python:3.13-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS build

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# Dependencies first, so source changes don't invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Randovania bundles a third-party Metroid Prime 2 randomizer for Windows,
# including zlib1.dll (zlib 1.2.8, CVE-2016-9840..9843, CVE-2018-25032). Only the
# Prime 1/2 exporters use it; this server is Dread-only (ADR 0001), so it never
# runs here. Remove it rather than ignore the finding (ADR 0010, decision 7).
RUN rm -rf /app/.venv/lib/python3.13/site-packages/randovania/data/ClarisPrimeRandomizer \
 && ! find /app/.venv -type f \( -iname '*.exe' -o -iname '*.dll' \) | grep .

# ---- runtime: no build tools, no uv, no source tree ----
FROM python:3.13-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS runtime

LABEL org.opencontainers.image.title="Dreadovania Server" \
      org.opencontainers.image.description="Self-hosted server for Metroid Dread Randovania seeds. Contains no game content." \
      org.opencontainers.image.source="https://github.com/nolsen311/dreadovania-server" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

# Debian publishes security fixes before the Python image is rebuilt with them, so
# apply them here: the image scan then reflects what Debian has already fixed,
# not when the base image was last rebuilt. Only the runtime stage ships.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get -y --no-install-recommends upgrade \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 dreadovania \
 && useradd --system --uid 10001 --gid dreadovania --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin dreadovania \
 && install -d -o 10001 -g 10001 -m 0750 /data

COPY --from=build /app/.venv /app/.venv
COPY LICENSE SOURCE.md /usr/share/doc/dreadovania/

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DREADOVANIA_DATA_DIR=/data

USER 10001:10001
WORKDIR /data
# 8081: the front door for pairing and invites, with secure remote access (ADR 0023).
EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-m", "dreadovania.healthcheck"]

ENTRYPOINT ["python", "-m", "dreadovania"]
