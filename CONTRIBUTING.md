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

## Security issues

Don't open public issues for vulnerabilities. See [`SECURITY.md`](SECURITY.md).
