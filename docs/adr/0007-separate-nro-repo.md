# 0007. Switch app lives in a separate repository

- Status: Accepted
- Date: 2026-11-07

## Context

The Switch homebrew app (NRO) uses a completely different toolchain
(devkitPro/libnx, C or C++) with its own build and release process.

## Decision

- The NRO lives in [`nolsen311/dreadovania-nx`](https://github.com/nolsen311/dreadovania-nx).
- **Patching always happens on the server.** The NRO pairs, picks or generates a seed,
  downloads the mod, installs or swaps it, and (later) swaps save data.
- This repo's **OpenAPI spec is the contract**. Breaking API changes need a new
  version prefix (`/api/v2`).
- The NRO reuses SimpleModManager code where its license and structure allow.

## Consequences

- Each repo has its own CI baseline, sized to its own toolchain.
- API compatibility has to be tested deliberately, with the spec checked in CI.
