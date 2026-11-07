# syntax=docker/dockerfile:1
#
# Every base image is pinned by multi-arch index digest, so the same line works
# for linux/amd64 and linux/arm64 (ADR 0003). Dependabot updates the digests.
# Both stages must use the same Python image: the virtualenv links to its interpreter.

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# ---- build: resolve the locked dependencies into a virtualenv ----
FROM python:3.13-slim-trixie@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 AS build

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

# ---- runtime: no build tools, no uv, no source tree ----
FROM python:3.13-slim-trixie@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 AS runtime

LABEL org.opencontainers.image.title="Dreadovania Server" \
      org.opencontainers.image.description="Self-hosted server for Metroid Dread Randovania seeds. Contains no game content." \
      org.opencontainers.image.source="https://github.com/nolsen311/dreadovania-server" \
      org.opencontainers.image.licenses="GPL-3.0-only"

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
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]

ENTRYPOINT ["python", "-m", "dreadovania"]
