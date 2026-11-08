# 0014. Release with a `/release` comment

- Status: Accepted
- Date: 2026-11-08
- Amends ADR 0009 (who creates release tags). Builds on ADR 0012 (`/signoff`).

## Context

A release is a `v*` tag, which the Release workflow builds, scans, signs and
publishes (ADR 0008). Under ADR 0009 only repository admins can create those
tags, and Claude never does, so releasing means running `git tag` and `git push`
on a computer. The maintainer wants to release from a phone, as they already
merge with `/signoff`, without giving Claude the power to publish.

## Decision

- When the **repository owner** comments exactly **`/release`** on a **merged**
  pull request, the *Release by comment* workflow:
  - checks that the pull request was merged into the default branch and that its
    merge commit is still in that branch's history;
  - checks that every check passed on the pull request's last commit (merges made
    by `/signoff` don't run CI on the default branch, so that commit is the tested
    one);
  - reads the version from `pyproject.toml` **at the merge commit**: the version
    isn't typed, so it can't be mistyped;
  - refuses a version that is already tagged or isn't newer than every release;
  - creates an annotated tag `v<version>` on the merge commit, recording who
    released it and from which pull request;
  - starts the Release workflow on that tag (`workflow_dispatch`), because a tag
    created with the workflow token doesn't trigger `push` workflows. The run's
    ref is the tag, so signatures and attestations name the same identity as a
    pushed tag (`release.yml@refs/tags/v…`).
- **The owner's comment is the authorisation.** It explains every refusal in a
  comment and reacts 👍 when the tag is created.
- The Release workflow accepts `workflow_dispatch` only on a version tag, and
  only from GitHub Actions (the `/release` workflow) or the owner.
- **Ruleset:** `protect-release-tags` adds GitHub Actions as a bypass actor, so
  the workflow token can create `v*` tags. Admins keep their bypass. Claude's
  GitHub access still can't create them.
- **Claude never posts `/release`** (CLAUDE.md), and still never creates or
  pushes tags. Claude's part of a release is the pull request that bumps the
  version.
- The same guards as `/signoff` apply: exact comment, created not edited, owner
  login and `OWNER` association; it never checks out or runs pull request code;
  the zizmor `dangerous-triggers` finding is suppressed on that one line, with
  this ADR as the maintainer's agreement.

## Consequences

- A release is two comments on the version-bump pull request: `/signoff`, then
  `/release`.
- Any workflow on the default branch with `contents: write` could now create a
  `v*` tag. Only workflows reviewed onto `main` run with that token, and this is
  the only one that creates tags; a workflow change that did so would be visible
  in review.
- Tags created this way are annotated and made by GitHub Actions, not signed
  with a personal key. The release's integrity rests, as before, on the keyless
  signature and provenance of the image, which name the tag.
- As with `/signoff`, stopping Claude from posting `/release` through the
  maintainer's account is a rule, not a technical control.
