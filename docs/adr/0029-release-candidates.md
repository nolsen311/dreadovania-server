# 0029. Release candidates: `X.Y.Z-rc.N`, never `latest`

- Status: Proposed
- Date: 2026-11-19
- Amends: [ADR 0014](0014-release-by-comment.md) (versions `/release` accepts)

## Context

The release workflow was rebuilt for multi-arch images (ADR 0028): an approval
job, native per-platform builds, then one signed index. It only runs from a
version tag, so its first run would otherwise be a real release. A release
candidate lets that run happen without moving the images people deploy.

Two things stood in the way. `/release` accepted only `X.Y.Z`, and the release
workflow always tagged the image `latest`.

## Decision

1. **Versions may be `X.Y.Z-rc.N`.** `/release` accepts them, with the version
   in `pyproject.toml` written exactly so, which is what the tag check compares.
   PEP 440 accepts it, and Python's packaging metadata normalizes it to
   `X.Y.ZrcN`.
2. **Ordering:** a candidate sorts after the previous release and before its
   own release; candidates sort numerically (`rc.10` after `rc.9`). `/release`
   refuses a version that isn't newer than every version tag, candidates
   included, so no candidate can follow its release.
3. **A candidate's image** gets only its own tag (`X.Y.Z-rc.N`): no `X.Y`, and
   no `latest`. Its Releases entry is a pre-release, and its notes start from the
   last release, like the release's will.
4. **Everything else is the same:** approval, scans, SBOMs, signatures and
   attestations.

## Consequences

- A change to the release process can be proved on a candidate first. 0.5.2-rc.1
  is the first, for ADR 0028's multi-arch release.
- Candidate tags stay in the registry and the repository. They're cheap, and
  deleting them would need repository settings changes.
