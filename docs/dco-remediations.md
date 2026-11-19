# DCO remediations

Commits on `main` that landed without a `Signed-off-by:` line, and the later
commit in which the maintainer signed off on them. History on `main` is never
rewritten, so a missing sign-off is fixed by a follow-up commit listed here.

A remediation takes effect when the commit that adds its entry is merged with
the maintainer's `Signed-off-by:` line. That sign-off certifies the Developer
Certificate of Origin 1.1 (see [`CONTRIBUTING.md`](../CONTRIBUTING.md)) for each
commit listed in the entry, as if it had been signed off at merge.

## 2026-11-07

Commits on `main` from before this repository adopted the DCO workflow: the
initial commit created on GitHub, the planning docs committed directly to
`main` before branch protection existed, and the squash merge of PR #1.

| Commit | Subject |
| --- | --- |
| `aeedea17234a59de33da1ecb04e62175e8ccaab5` | Initial commit |
| `44272fc9c10b88624e32ba31f7e288e4dcdba7ae` | docs: add project plan, ADRs and main-branch ruleset |
| `22dc8fea6ac85419fd778fd041ef72f2834b5e36` | Add server skeleton, hardened container image and CI/CD pipeline (#1) |

## 2026-11-07 (PR #7)

The squash merge of PR #7 was made without the maintainer's `Signed-off-by:`
line in the commit message (the PR checklist line mentioning it is not a
sign-off; `DCO audit` only accepts a line that starts with `Signed-off-by:`).

| Commit | Subject |
| --- | --- |
| `19c1fb8a1fcfc983a8c20398557e2ca787d66611` | Phase 1, PR 2: sign-in, with every API route protected by default (#7) |

## 2026-11-19 (PRs #37, #38, #89, #90, #91, #92)

The squash merges of these pull requests were made without the maintainer's
`Signed-off-by:` line in the commit message (not through `/signoff`, which adds
it). `scripts/bootstrap.sh` listed the first five on 2026-11-19; #92 merged the
same way minutes later.

| Commit | Subject |
| --- | --- |
| `48423a5877cc739da49f42109f7e253c92feb32e` | Icons that fit installed-app launchers: maskable and themed (#37) |
| `2e8e079f0225c72f6e8acb398928f3279c04fb57` | Seed download for the Switch app: a manifest and one tar (ADR 0018) (#38) |
| `c94f91bb23dc52470bf2773c94ded034876e4643` | Publish arm64 images, each platform built natively (ADR 0028) (#89) |
| `515c02e881bad260de1b2e888db199fcd195e4e0` | Require "Container image (arm64)" on main (#90) |
| `410d13b8e94127bbd252bf16c21885e669530348` | Release candidates: X.Y.Z-rc.N, never latest (ADR 0029) (#91) |
| `ed4256454199fad7b1684b207e6a9a6dc686408b` | Release 0.5.2-rc.1 (#92) |
