# Working rules for Claude

These rules apply to every Claude session and workflow in this repository.
They exist so that the project's history is a trustworthy audit trail.

## Authority and sign-off

- **Never add a `Signed-off-by:` line** to any commit, for anyone. A sign-off is a
  personal legal certification under the Developer Certificate of Origin. The
  maintainer signs off on Claude-authored changes when merging.
- **Never merge a pull request, approve one, or create or push a tag.** Those are
  the maintainer's actions, even when tools would technically allow them.
- **Never post a `/signoff` comment**, or anything that starts with `/signoff`.
  It merges with the maintainer's sign-off (ADR 0012); only they may post it.
- **Never change repository settings, rulesets, environments or secrets.**
  Propose changes in a pull request (for example to `.github/rulesets/`) and
  let the maintainer apply them.
- Commit as `Claude <noreply@anthropic.com>`, so authorship is visible in history.
  End commit messages with the `Co-Authored-By:` and session trailers.

## How changes are made

- Never push to `main`. Work on a branch and open a pull request.
- Keep pull request descriptions complete: what changed, why, and how it was
  verified. They are the durable record; session links are not.
- Record significant decisions as ADRs in `docs/adr/`. To change a decision,
  add a new ADR that supersedes the old one.
- Never weaken a check to get CI green: no skipped, disabled or quarantined tests,
  and no loosened rulesets or scanner thresholds without the maintainer's
  explicit agreement in the pull request.
- Report results faithfully. If something wasn't verified, say so.

## Supply chain

- Pin every GitHub Action to a full commit SHA, with the version in a comment.
- Workflows start from `permissions: {}`; grant permissions per job.
- Pin container images by digest, and verify digests against the registry
  rather than trusting a single lookup.
- New dependencies need a stated reason in the pull request.

## Source files

- New source files start with an SPDX header:
  `SPDX-License-Identifier: GPL-3.0-or-later`.
- Never commit secrets, keys, credentials or personal data.

## This project

- **Content policy (absolute):** never add game content, console keys, extracted
  RomFS/ExeFS trees, or links to them. Users supply their own files. The
  `Content guard` check enforces this; never loosen it. See `docs/PLAN.md` section 3.
- **Branding:** "Dreadovania". No "Metroid" in names, and no Nintendo artwork.
- **Scope:** Metroid Dread only (ADR 0001). Randovania goes through its CLI, or one
  adapter module where the CLI is not enough (ADR 0002).
- **arm64-ready:** no x86-only binaries or URLs; resolve with `TARGETARCH` (ADR 0003).
- **API contract:** after changing the API, regenerate `docs/openapi.json` with
  `uv run python scripts/export_openapi.py`. Breaking changes need a new `/api/vN`.
- **Before pushing,** run:
  `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv lock --check && uv run python scripts/export_openapi.py --check`
- The plan and every accepted decision are in `docs/PLAN.md` and `docs/adr/`.
