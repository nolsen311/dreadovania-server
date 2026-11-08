# Contributing

## How changes are made

This project is written by Claude under the maintainer's oversight:

1. Claude works on a branch, as the commit author `Claude <noreply@anthropic.com>`,
   and opens a pull request.
2. CI runs the required checks, including the DCO check below.
3. The maintainer reviews the change, then **squash-merges it with their own
   `Signed-off-by:` line in the merge commit message**. That sign-off is the
   maintainer's certification of the change.

Outside contributions are welcome under the same rules: a pull request with
every commit signed off.

## Developer Certificate of Origin

All commits must be signed off, certifying the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):
that you wrote the change or otherwise have the right to submit it under the
project's license.

Sign off by committing with `-s`:

```
git commit -s -m "Describe the change"
```

This adds a line such as:

```
Signed-off-by: Your Name <you@example.com>
```

You may use a pseudonym and a GitHub `users.noreply.github.com` address. The
email in your sign-off must match the commit's author email.

The `DCO` check enforces this on every pull request:

- GitHub bots such as Dependabot are exempt.
- Claude-authored commits are exempt **only in pull requests opened by the
  repository owner**, because the maintainer signs off on them at merge.
  Outside contributors can't use this exemption.

If the check fails, add sign-offs with `git rebase --signoff <base>` and
force-push your branch.

### Signing off when merging (maintainer)

**From anywhere, including a phone:** comment exactly `/signoff` on the pull
request. The *Sign off and merge* workflow then squash-merges it with the PR
title and description, `Co-authored-by:` lines for the commit authors, and your
`Signed-off-by:` line (your GitHub name and no-reply address). It refuses, with a
comment saying why, if any required check hasn't passed, the PR has conflicts or
is a draft, or new commits arrived after your comment. A 👍 on your comment means
it merged. Only the repository owner can trigger it (see ADR 0012).

**By hand on the web:**

GitHub's "Require sign-off on web-based commits" setting does **not** add a
sign-off to squash merges. When squash-merging, add your line to the end of
the commit message by hand:

```
Signed-off-by: Your Name <you@example.com>
```

A commit that reaches `main` without a sign-off is flagged by the `DCO audit`
job. History on `main` is never rewritten: the maintainer signs off on it in a
follow-up pull request that adds an entry to
[`docs/dco-remediations.md`](docs/dco-remediations.md).

## Releasing (maintainer)

1. A pull request bumps `version` in `pyproject.toml` (and `uv.lock`). Ask
   Claude for "release X.Y.Z" and it opens one.
2. Comment `/signoff` on it, as for any change.
3. Once it has merged, comment exactly **`/release`** on the same pull request.
   The *Release by comment* workflow tags the merge commit `vX.Y.Z` (the version
   in `pyproject.toml` at that commit) and starts the Release workflow on the
   tag. It refuses, with a comment saying why, if the pull request isn't merged,
   its checks didn't pass, or the version is already released or isn't newer
   than the latest release. A 👍 on your comment means the tag was created.
   Only the repository owner can trigger it (see ADR 0014).
4. When the Release workflow finishes, verify the image as `SECURITY.md`
   describes and deploy it by digest.

Pushing a `v*` tag yourself still works too.

## Security issues

Don't open public issues for vulnerabilities. See [`SECURITY.md`](SECURITY.md).
