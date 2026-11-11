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

**Stacked pull requests** (ADR 0024): sign off from the bottom up. Comment
`/signoff` on the bottom PR, the one that targets `main`. Once it has merged,
change the next PR's base to `main` (or ask Claude to), then comment `/signoff`
on that one. A `/signoff` on a PR that doesn't target `main` is refused, because
merging it would also merge the PRs below it without your sign-off.

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
   in `pyproject.toml` at that commit) with the release app (below), and the
   new tag starts the Release workflow. It refuses, with a comment saying why, if the pull request isn't merged,
   its checks didn't pass, or the version is already released or isn't newer
   than the latest release. A 👍 on your comment means the tag was created.
   Only the repository owner can trigger it (see ADRs 0014 and 0015).
4. Approve the `release` environment's deployment on the Release run's page.
   When the run finishes, it also creates the version's entry on the Releases
   page: the image's tag and digest, the verify command, and notes generated
   from the pull requests merged since the previous version tag.
5. Verify the image as `SECURITY.md` describes and deploy it by digest.

A version released before the workflow created release entries (0.1.1, 0.1.2)
can be given one by hand, without creating a tag:

```bash
gh release create v0.1.2 --verify-tag --generate-notes --notes-start-tag v0.1.1
```

Pushing a `v*` tag yourself still works too.

### Release app (one-time setup)

The tag ruleset lets only admins and your **release app** create `v*` tags; the
workflow's own token can't be exempted on a personal account (ADR 0015).

1. **Create the app:** <https://github.com/settings/apps/new>
   - **Name:** anything unique, e.g. `dreadovania-releaser-<you>`.
   - **Homepage URL:** this repository's URL.
   - **Webhook:** untick **Active**.
   - **Repository permissions:** **Contents: Read and write**. Nothing else.
   - **Where can this GitHub App be installed?** **Only on this account.**
2. **On the app's page:** note the **App ID** and **Client ID**, then
   **Generate a private key** (a `.pem` file downloads).
3. **Install App** → **Only select repositories** → this repository.
4. **Store the settings** (from a clone, with `gh` logged in as you):

   ```bash
   gh variable set RELEASE_APP_ID --body <App ID>
   ```

   ```bash
   gh variable set RELEASE_APP_CLIENT_ID --body <Client ID>
   ```

   ```bash
   scripts/bootstrap.sh <owner>/<repo>
   ```

   ```bash
   gh secret set RELEASE_APP_PRIVATE_KEY --env release-tag < path/to/key.pem
   ```

   `bootstrap.sh` creates the `release-tag` environment (main only) and adds the
   app to the tag ruleset's bypass list. Then delete the `.pem` file, or keep it
   only in a password manager: GitHub can generate a new key at any time.

## Security issues

Don't open public issues for vulnerabilities. See [`SECURITY.md`](SECURITY.md).
