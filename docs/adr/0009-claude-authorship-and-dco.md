# 0009. Claude authorship, maintainer sign-off (DCO)

- Status: Accepted
- Date: 2026-11-07
- Adopted from [`claude-project-template`](https://github.com/nolsen311/claude-project-template) (its ADR 0002).

## Context

The code is written by Claude with the maintainer's oversight. The project
needs an audit trail that shows who wrote each change and that a human
certified it. In Claude Code sessions, Claude pushes and opens pull requests
through the maintainer's GitHub account, so GitHub cannot require a separate
approval: nobody can approve their own pull request.

## Decision

- Claude commits as `Claude <noreply@anthropic.com>`, so authorship is visible in
  git history regardless of which account pushed.
- The maintainer certifies each change by squash-merging it with their own
  `Signed-off-by:` line (Developer Certificate of Origin 1.1). GitHub signs
  web merges, recording that the merge was done from the maintainer's account.
- The `DCO` check exempts Claude-authored commits only in pull requests opened
  by the repository owner. The `DCO audit` job flags a merge without a
  sign-off; it is remediated in `docs/dco-remediations.md`, never by rewriting history.
- Rulesets on `main`: pull requests only, squash merge only, signed commits,
  linear history, required checks. **No required approving review.**
- Release tags (`v*`) can only be created by repository admins (and, since
  ADR 0014, by the `/release` workflow on the owner's comment).
- `CLAUDE.md` forbids Claude from signing off, merging, approving, tagging, or
  changing repository settings.

## Consequences

- The record on `main` reads: Claude wrote it (author), the maintainer certified
  and merged it (sign-off and a GitHub-signed merge commit), CI checked it.
- That a human merged each change is a rule, not a technical control: Claude
  acts through the maintainer's credentials and could technically merge.
  `CLAUDE.md` and `DCO audit` mitigate this.
- **Upgrade path**, if technical enforcement is needed later: give Claude its
  own per-project GitHub App identity (used through `claude-code-action`),
  require one code-owner approval plus last-push approval in the main ruleset,
  and keep the app off every bypass list. This needs a superseding ADR.
