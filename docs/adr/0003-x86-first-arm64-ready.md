# 0003. x86-64 first, arm64-ready

- Status: Accepted
- Date: 2026-11-07

## Context

The first host is x86-64. arm64 hosts (Raspberry Pi, ARM NAS) are a likely
later target, and we don't want choices now that would need reworking for them.

## Decision

- Publish `linux/amd64` only for now.
- **Base image:** Debian slim (glibc) with the official multi-arch Python image,
  pinned by digest. No Alpine, because musl breaks many Python wheels.
- **Binaries:** none fetched from a URL hardcoded for x86; resolve them with
  `TARGETARCH` in the Dockerfile.
- **Dependencies:** the `uv` lockfile must resolve for both platforms.
- **CI:** build with `docker buildx`, with the target platforms in one variable.
- **Test build:** a weekly arm64 build that doesn't block and publishes nothing.
  When it fails, it opens or updates a tracking issue.

## Consequences

- Adding arm64 should take a one-line change to the platforms variable.
- If a dependency stops supporting arm64, we find out within a week, not at release time.
