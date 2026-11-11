# 0024. `/signoff` for stacked pull requests

- Status: Proposed
- Date: 2026-11-11
- Amends: [ADR 0012](0012-signoff-by-comment.md) (how the merge is made)

## Context

ADR 0012's workflow squash-merges with GitHub's merge endpoint
(`PUT /pulls/{n}/merge`). When pull requests are stacked on GitHub (each one's
base is the branch of the one below), GitHub refuses that endpoint for every PR
in the stack, the bottom one included:

> Merging stacked PRs via this endpoint is not supported. Use the asynchronous
> merge endpoint instead.

That happened with #61 and #62 on 2026-11-11, which then had to be merged by
hand on the web, with the sign-off typed into each message.

The asynchronous endpoint (`PUT /pulls/{n}/merge-async`, then
`GET /pulls/{n}/merge-async/{uuid}` for the result) is GitHub's required way to
merge stacked PRs. Its documentation says two things that matter here:

- For a stacked PR, "the operation includes all open downstack pull requests":
  merging the top of a stack merges the ones below it too.
- A custom commit title and message are "only supported for direct merges"
  (`merge_action: direct_merge`), not through a merge queue.

## Decision

1. **The workflow merges through the asynchronous endpoint**, for every PR,
   stacked or not, with `merge_action: direct_merge`, `merge_method: squash`,
   `bypass_rules: false`, and the head `sha` it inspected, as before. It waits
   for the result (about two minutes at most) and reports a failure, or a merge
   still running, in a comment.
2. **`/signoff` still only merges a PR that targets the default branch.** In a
   stack that is the bottom PR, which has nothing below it, so only that one PR
   is merged, with the message and sign-off the workflow wrote. A `/signoff` on a
   PR higher up is refused, saying to sign off the one below first. This keeps
   one sign-off per commit on `main`: merging the top of a stack would merge the
   PRs below with messages GitHub wrote, without the owner's `Signed-off-by:`.
3. **Stacks are signed off from the bottom up**: `/signoff` the bottom PR; once it
   has merged, the next PR's base is changed to the default branch (on GitHub,
   or by Claude, who also merges the default branch into it so its diff is only
   its own), and that one gets its own `/signoff`.
4. **The workflow checks the commit GitHub made.** After the merge, it reads the
   new commit on the default branch and confirms it ends with the owner's
   `Signed-off-by:` line. If not, it says so in a comment, without the 👍, so it
   is recorded as a DCO remediation instead of going unnoticed.

Everything else in ADR 0012 stands: who may trigger it, the exact comment, never
running PR code, the head-commit pin, and Claude never posting `/signoff`.

## Consequences

- `/signoff` works for stacked PRs, one PR at a time from the bottom.
- Signing off a whole stack takes one comment per PR, plus a base change between
  them. That is deliberate: each commit on `main` carries its own sign-off.
- The workflow now waits for GitHub to finish the merge, so a run takes a little
  longer, and a merge GitHub is still working on is reported as "not merged yet"
  (it may finish afterwards; the PR page shows it).
