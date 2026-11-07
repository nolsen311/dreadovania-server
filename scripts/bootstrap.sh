#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# One-time (and safe to re-run) setup for a repository created from
# claude-project-template. Configures everything a template can't copy.
#
# Run it yourself, as the repository owner. Claude must never run it: it
# changes repository settings, which CLAUDE.md reserves for the maintainer.
#
# Usage:
#   scripts/bootstrap.sh OWNER/REPO [--dry-run] [--template]
#
#   --dry-run   Read-only lookups still run; every change is printed, not made.
#   --template  Also mark the repository as a template (for the template repo itself).
#
# Requires: gh (logged in as the repository owner), jq. Run from a clone of the
# repository so the rulesets in .github/rulesets/ can be read.

set -euo pipefail

usage() { sed -n '9,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

REPO=""
DRY_RUN=0
TEMPLATE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --template) TEMPLATE=1 ;;
    -h|--help) usage ;;
    */*) REPO="$arg" ;;
    *) echo "Unknown argument: $arg" >&2; usage ;;
  esac
done
[ -n "$REPO" ] || usage

WARNINGS=()
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok    %s\n' "$*"; }
warn() { printf '   WARN  %s\n' "$*"; WARNINGS+=("$*"); }

# Read-only API call: always runs.
get() { gh api "$@"; }

# Mutating API call: printed in dry-run mode, otherwise run. Output is discarded;
# failures are reported by the caller.
mutate() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '   would gh api %s\n' "$*"
    return 0
  fi
  gh api "$@" >/dev/null
}

# ---- prerequisites ----------------------------------------------------------
step "Checking prerequisites"
for tool in gh jq; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "Not logged in: run 'gh auth login' first." >&2; exit 1; }
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
[ -d "$ROOT/.github/rulesets" ] || { echo "Run this from a clone of the repository (no .github/rulesets found)." >&2; exit 1; }

is_admin="$(get "repos/$REPO" --jq '.permissions.admin')"
[ "$is_admin" = "true" ] || { echo "You need admin rights on $REPO." >&2; exit 1; }
OWNER_LOGIN="$(get user --jq '.login')"
OWNER_ID="$(get user --jq '.id')"
VISIBILITY="$(get "repos/$REPO" --jq '.visibility')"
ok "gh logged in as $OWNER_LOGIN, admin on $REPO ($VISIBILITY)"
[ "$DRY_RUN" -eq 1 ] && ok "dry run: no changes will be made"

# ---- merge settings ---------------------------------------------------------
step "Merge settings: squash only, PR title and body as the commit message"
args=(-X PATCH "repos/$REPO"
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false
  -F allow_auto_merge=false -F delete_branch_on_merge=true
  -F web_commit_signoff_required=true
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY)
[ "$TEMPLATE" -eq 1 ] && args+=(-F is_template=true)
if mutate "${args[@]}"; then ok "merge settings applied"; else warn "could not update merge settings"; fi

# ---- security features ------------------------------------------------------
step "Security features"
if mutate -X PUT "repos/$REPO/vulnerability-alerts"; then ok "Dependabot alerts on"; else warn "could not enable Dependabot alerts"; fi
if mutate -X PUT "repos/$REPO/automated-security-fixes"; then ok "Dependabot security updates on"; else warn "could not enable Dependabot security updates"; fi
if mutate -X PUT "repos/$REPO/private-vulnerability-reporting"; then ok "private vulnerability reporting on"; else warn "could not enable private vulnerability reporting"; fi
if mutate -X PATCH "repos/$REPO" --input - <<'JSON'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}}
JSON
then ok "secret scanning and push protection on"
else warn "could not enable secret scanning (private repositories may need GitHub Advanced Security)"; fi

# ---- GitHub Actions ---------------------------------------------------------
step "GitHub Actions: read-only default token, full-SHA pinning required"
if mutate -X PUT "repos/$REPO/actions/permissions/workflow" \
     -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false; then
  ok "default workflow token is read-only and cannot approve PRs"
else
  warn "could not set default workflow permissions"
fi
if mutate -X PUT "repos/$REPO/actions/permissions" \
     -F enabled=true -f allowed_actions=all -F sha_pinning_required=true; then
  ok "actions must be pinned to a full commit SHA"
else
  warn "could not require SHA pinning (set it under Settings > Actions > General)"
fi

# ---- release environment ----------------------------------------------------
step "Environment 'release': you approve every deployment; only v* tags"
if mutate -X PUT "repos/$REPO/environments/release" --input - <<JSON
{"reviewers": [{"type": "User", "id": $OWNER_ID}],
 "prevent_self_review": false,
 "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
then
  ok "release environment requires approval from $OWNER_LOGIN"
  existing_policy="$(get "repos/$REPO/environments/release/deployment-branch-policies" \
      --jq '.branch_policies[] | select(.name == "v*" and .type == "tag") | .id' 2>/dev/null || true)"
  if [ -n "$existing_policy" ]; then
    ok "tag policy v* already present"
  elif mutate -X POST "repos/$REPO/environments/release/deployment-branch-policies" -f name='v*' -f type=tag; then
    ok "release environment limited to v* tags"
  else
    warn "could not add the v* tag policy to the release environment"
  fi
else
  warn "could not create the release environment"
fi

# ---- rulesets ---------------------------------------------------------------
step "Rulesets from .github/rulesets/"
existing_rulesets="$(get "repos/$REPO/rulesets" --jq '[.[] | {name, id}]')"
for file in "$ROOT"/.github/rulesets/*.json; do
  name="$(jq -r '.name' "$file")"
  id="$(jq -r --arg n "$name" '.[] | select(.name == $n) | .id' <<<"$existing_rulesets")"
  if [ -n "$id" ]; then
    if mutate -X PUT "repos/$REPO/rulesets/$id" --input "$file"; then ok "updated ruleset $name"; else warn "could not update ruleset $name"; fi
  else
    if mutate -X POST "repos/$REPO/rulesets" --input "$file"; then ok "created ruleset $name"; else warn "could not create ruleset $name"; fi
  fi
done

# ---- Claude GitHub App access -----------------------------------------------
step "Claude GitHub App: grant access to $REPO"
installation_id="$(get user/installations --paginate \
    --jq '.installations[] | select(.app_slug == "claude") | .id' 2>/dev/null | head -n1 || true)"
repo_id="$(get "repos/$REPO" --jq '.id')"
if [ -z "$installation_id" ]; then
  warn "Claude GitHub App installation not found; install it from https://github.com/apps/claude"
elif mutate -X PUT "user/installations/$installation_id/repositories/$repo_id"; then
  ok "Claude GitHub App can access $REPO"
else
  warn "could not add $REPO to the Claude app; add it under Settings > Applications > Claude > Configure"
fi

# ---- DCO remediation entry for commits already on main ----------------------
step "Commits on the default branch without a sign-off"
branch="$(get "repos/$REPO" --jq '.default_branch')"
# shellcheck disable=SC2016  # \(...) below is jq string interpolation, not shell.
unsigned="$(get "repos/$REPO/commits?sha=$branch&per_page=100" \
  --jq '.[] | select(.commit.message | test("(?im)^Signed-off-by:") | not)
             | select(.commit.author.email | test("\\[bot\\]@users\\.noreply\\.github\\.com$") | not)
             | "| `\(.sha)` | \(.commit.message | split("\n")[0]) |"' 2>/dev/null || true)"
if [ -z "$unsigned" ]; then
  ok "every commit on $branch is signed off"
else
  echo "   Add this entry to docs/dco-remediations.md in your first pull request,"
  echo "   then sign off when you merge it:"
  echo
  echo "## $(date +%F)"
  echo
  echo "Commits created before the DCO workflow applied (for example the initial"
  echo "commit created from the project template)."
  echo
  echo "| Commit | Subject |"
  echo "| --- | --- |"
  echo "$unsigned"
fi

# ---- summary ----------------------------------------------------------------
step "Done"
if [ "${#WARNINGS[@]}" -eq 0 ]; then
  ok "all steps succeeded"
else
  echo "   ${#WARNINGS[@]} step(s) need attention:"
  for w in "${WARNINGS[@]}"; do echo "   - $w"; done
fi
