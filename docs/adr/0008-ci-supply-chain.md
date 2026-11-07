# 0008. CI/CD supply-chain baseline

- Status: Accepted
- Date: 2026-11-07

## Context

Every published build must be verifiably safe for users to run on their home network.

## Decision

Adopt the CI/CD baseline in [`docs/PLAN.md` §7](../PLAN.md#7-cicd-supply-chain). In particular:

- Every action pinned to a full SHA, with minimal per-job permissions.
- `uv` lockfile with hashes; `pip-audit`, OSV-Scanner and CodeQL.
- Images pinned by digest, built with buildx, and scanned with Grype. Releases push by
  digest, scan every platform, and apply tags only after the scan passes.
- A content guard that rejects game files and keys.
- Releases built from tags only: SBOM, SLSA provenance, cosign keyless signing, published to GHCR.
- OpenSSF Scorecard.
- **Dependabot** rather than Renovate. It now covers uv, Docker digests and Actions,
  and it avoids adding a third-party app to the supply chain.
- Protection on `main` via a repository ruleset, kept in
  [`.github/rulesets/protect-main.json`](../../.github/rulesets/protect-main.json).

## Consequences

- Only the initial planning docs, README and ruleset file were committed directly to
  `main`, before protection was enabled. Everything after that goes through pull requests.
- Required status checks get added to the ruleset once the CI workflows exist.
