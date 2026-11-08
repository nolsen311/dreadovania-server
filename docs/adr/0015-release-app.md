# 0015. Release tags are created by the maintainer's release app

- Status: Accepted
- Date: 2026-11-08
- Amends ADR 0014 (how `/release` creates the tag).

## Context

ADR 0014 had the `/release` workflow create the tag with the workflow's own
token, with "GitHub Actions" added to the tag ruleset's bypass list. GitHub
rejected that bypass entry: *"Actor GitHub Actions integration must be part of
the ruleset source or owner organization"*. On a repository owned by a personal
account, the workflow token can't be exempted, so the tag ruleset (correctly)
refused every tag the workflow tried to create.

## Decision

- The maintainer owns a small **GitHub App** (the "release app"), installed on
  this repository only, with one permission: **Contents: read and write**. No
  webhook, installable only on the maintainer's account.
- The tag ruleset's bypass list holds repository admins and that app.
  `scripts/bootstrap.sh` adds the app (from the repository variable
  `RELEASE_APP_ID`) whenever it applies the rulesets, so re-running it never
  drops the app.
- The `/release` workflow mints a **short-lived installation token** from the app
  (`actions/create-github-app-token`, pinned), scoped to this repository and to
  `contents: write`, and uses it only to create the tag. Everything else
  (reading the pull request, its checks and tags, commenting) uses the
  workflow's own token, which is now read-only for contents.
- The app's private key is a secret of the **`release-tag` environment**, which
  only the default branch can use (the workflow runs from it on
  `issue_comment`). Its ID and client ID are repository variables (not secret).
- A tag created with an app token triggers `push: tags` workflows, so the
  Release workflow starts from the tag as it does for a pushed tag. The
  `workflow_dispatch` route from ADR 0014 is removed.
- If the app isn't set up, `/release` says so on the pull request and points to
  the setup steps (`CONTRIBUTING.md`, "Release app"). It never falls back to the
  workflow's token.

Considered and rejected:

- **A personal access token** as a secret: long-lived, and acts as the
  maintainer everywhere it's allowed.
- **Dropping the "creation" rule** from the tag ruleset: anyone with write access
  could then create release tags.

## Consequences

- One-time manual setup by the maintainer (GitHub has no API to create an app):
  create the app, generate its key, install it, set two variables and one
  secret, re-run `bootstrap.sh`.
- The app's key can create and move `v*` tags in this repository (and write
  contents, though branch rules still apply to `main`). It is only available to
  workflows running from the default branch, through the `release-tag`
  environment, and `/release` is the only workflow that uses it.
- Tags show the app as their creator; the annotated tag message records the
  maintainer who released it and from which pull request.
