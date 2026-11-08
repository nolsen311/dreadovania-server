# 0012. Sign off and merge with a `/signoff` comment

- Status: Accepted
- Date: 2026-11-08

## Context

Under ADR 0009 the maintainer certifies each change by adding their
`Signed-off-by:` line to the squash-merge message. That line is easy to forget,
and editing the squash message isn't practical from GitHub's mobile app. Twice
already a merge reached `main` without it and needed a remediation entry.

## Decision

- When the **repository owner** comments exactly **`/signoff`** on a pull
  request, the *Sign off and merge* workflow squash-merges it. The commit
  message is the PR title and description, `Co-authored-by:` lines for the
  commit authors, and the owner's `Signed-off-by:` line built from their GitHub
  profile (name, and `<id>+<login>@users.noreply.github.com`).
- **The owner's comment is the certification.** The workflow only writes it down.
- **Guards:**
  - The comment must be exactly `/signoff`, created (not edited), by the
    repository owner (login equals the owner, and author association `OWNER`).
  - It never checks out or runs pull request code. `issue_comment` always runs the
    workflow from the default branch, and the job checks out only the default
    branch (the script in `.github/scripts/`).
  - It merges only when GitHub reports the PR `clean` (required checks passed,
    no conflicts, not a draft, targeting the default branch).
  - It merges only the head commit it inspected (`sha` on the merge call), and
    refuses if the head commit is newer than the comment.
  - Trailer-like lines (`Signed-off-by`, `Co-authored-by`) in the PR description
    are dropped, so the only sign-off in the message is the owner's.
- It explains every refusal in a comment, and reacts 👍 on success.
- **Claude never posts `/signoff`** (CLAUDE.md).
- **zizmor** flags `issue_comment` as a dangerous trigger, because such workflows
  usually run untrusted PR code with write access. This one doesn't, for the
  reasons above, so that finding is suppressed on that one line, with this ADR
  as the record of the maintainer's agreement.

## Consequences

- The maintainer can certify and merge from a phone, and the sign-off can no
  longer be forgotten.
- Merges made with the workflow token don't trigger further workflow runs, so
  `DCO audit` and push-triggered scans don't run for those merges. The sign-off
  is written by construction, and the scheduled CodeQL and Scorecard runs still
  cover `main`.
- As with merging (ADR 0009), stopping Claude from posting `/signoff` through the
  maintainer's account is a rule, not a technical control.
- The "head commit newer than the comment" check uses the commit's committer
  date, which a pusher can set; the `sha`-pinned merge is the firm guarantee,
  covering changes between the check and the merge.
