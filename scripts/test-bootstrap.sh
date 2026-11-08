#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tests for scripts/bootstrap.sh against a fake `gh` that behaves like the real
# one on errors: the error JSON goes to stdout, a message to stderr, exit 1.
# No network access and no real GitHub calls.
#
# Usage: scripts/test-bootstrap.sh

set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/bootstrap.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

# ---- fake gh -----------------------------------------------------------------
mkdir -p "$WORK/bin"
cat > "$WORK/bin/gh" <<'FAKE'
#!/usr/bin/env bash
echo "gh $*" >> "$FAKE_LOG"
[ "$1" = auth ] && exit 0
shift
method=GET; jqf=""; path=""
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    --jq) jqf="$2"; shift 2 ;;
    --paginate) shift ;;
    -f|-F) shift 2 ;;
    --input) if [ "$2" = - ]; then cat >> "$FAKE_LOG"; else cat "$2" >> "$FAKE_LOG"; fi; shift 2 ;;
    *) path="$1"; shift ;;
  esac
done
http_error() {  # like the real gh: body on stdout, message on stderr, exit 1
  printf '{"message":"%s","status":"%s"}\n' "$2" "$1"
  echo "gh: $2 (HTTP $1)" >&2
  exit 1
}
if [ "$method" != GET ]; then
  if [ -n "${FAIL_PATH:-}" ] && [[ "$path" == *"$FAIL_PATH"* ]]; then http_error 422 "Validation Failed"; fi
  exit 0
fi
case "$path" in
  repos/o/r) body='{"id":42,"visibility":"public","default_branch":"main","permissions":{"admin":true}}' ;;
  user) body='{"login":"maintainer","id":1}' ;;
  user/installations) http_error 403 "You must authenticate with an access token authorized to a GitHub App" ;;
  repos/o/r/rulesets) body='[{"name":"protect-main","id":11}]' ;;
  repos/o/r/environments/release/deployment-branch-policies) http_error 404 "Not Found" ;;
  repos/o/r/actions/variables/RELEASE_APP_ID)
    if [ -n "${FAKE_APP_ID:-}" ]; then body="{\"name\":\"RELEASE_APP_ID\",\"value\":\"$FAKE_APP_ID\"}"
    else http_error 404 "Not Found"; fi ;;
  repos/o/r/commits*) body='[
    {"sha":"1111111111111111111111111111111111111111","commit":{"message":"Initial commit","author":{"email":"m@example.com"}}},
    {"sha":"2222222222222222222222222222222222222222","commit":{"message":"Already remediated","author":{"email":"m@example.com"}}},
    {"sha":"3333333333333333333333333333333333333333","commit":{"message":"Fix\n\nSigned-off-by: M <m@example.com>","author":{"email":"m@example.com"}}},
    {"sha":"4444444444444444444444444444444444444444","commit":{"message":"Bump","author":{"email":"1+dependabot[bot]@users.noreply.github.com"}}}]' ;;
  *) http_error 404 "Not Found" ;;
esac
if [ -n "$jqf" ]; then jq -r "$jqf" <<<"$body"; else echo "$body"; fi
FAKE
chmod +x "$WORK/bin/gh"

# ---- a throwaway repository to run the script from ---------------------------
REPO_DIR="$WORK/repo"
mkdir -p "$REPO_DIR/.github/rulesets" "$REPO_DIR/docs"
echo '{"name":"protect-main"}' > "$REPO_DIR/.github/rulesets/protect-main.json"
echo '{"name":"protect-release-tags"}' > "$REPO_DIR/.github/rulesets/protect-release-tags.json"
# shellcheck disable=SC2016  # the backticks are literal Markdown, not command substitution.
printf '| `%s` | Already remediated |\n' 2222222222222222222222222222222222222222 > "$REPO_DIR/docs/dco-remediations.md"
git -C "$REPO_DIR" init -q

export PATH="$WORK/bin:$PATH" FAKE_LOG="$WORK/gh.log"

run() {  # run bootstrap.sh in the throwaway repo; output in $WORK/out
  : > "$FAKE_LOG"
  (cd "$REPO_DIR" && "$SCRIPT" o/r "$@") > "$WORK/out" 2>&1
}
check() {  # check DESCRIPTION COMMAND...
  local desc="$1"; shift
  if "$@"; then echo "pass  $desc"; else echo "FAIL  $desc"; FAILURES=$((FAILURES + 1)); fi
}
has()    { grep -qF -- "$1" "$WORK/out"; }
hasnt()  { ! grep -qF -- "$1" "$WORK/out"; }
called() { grep -qF -- "$1" "$FAKE_LOG"; }

# ---- dry run -----------------------------------------------------------------
run --dry-run
check "dry run makes no changes"                     bash -c "! grep -q ' -X ' '$FAKE_LOG'"
check "dry run says 'plan', not 'ok', for changes"   has "plan  merge settings applied"
check "dry run never claims a change was made"       hasnt "ok    merge settings applied"
check "GitHub error JSON never appears as data"      hasnt '"message"'
check "Claude app access is a manual TODO"           has "TODO  add o/r to the Claude app"
check "installations API is not called"              bash -c "! grep -q 'user/installations' '$FAKE_LOG'"
check "missing tag policy is not reported present"   hasnt "tag policy v* already present"
check "unsigned commit is listed for remediation"    has "1111111111111111111111111111111111111111"
check "recorded commit is not listed again"          hasnt "2222222222222222222222222222222222222222"
check "signed-off commit is not listed"              hasnt "3333333333333333333333333333333333333333"
check "bot commit is not listed"                     hasnt "4444444444444444444444444444444444444444"

# ---- real run with one failing change ----------------------------------------
FAIL_PATH=private-vulnerability-reporting run
check "a failing change is reported as WARN"         has "WARN  could not enable private vulnerability reporting"
check "later steps still run after a failure"        called "-X POST repos/o/r/rulesets"
check "existing ruleset is updated in place"         called "-X PUT repos/o/r/rulesets/11"
check "real run reports changes as ok"               has "ok    merge settings applied"
check "summary lists the manual step"                has "Manual step(s):"
check "release-tag environment is created"           called "-X PUT repos/o/r/environments/release-tag"
check "without the app, setting it up is a TODO"     has "TODO  create the release app"
check "without the app, no bypass is added"          bash -c "! grep -q 'Integration' '$FAKE_LOG'"

# ---- real run once the release app exists ------------------------------------
FAKE_APP_ID=777 run
check "the app is reported"                          has "release app 777 will be allowed to create v* tags"
check "the app is added to the tag ruleset's bypass" bash -c "grep -q '\"actor_id\": 777' '$FAKE_LOG'"
check "no app TODO once it exists"                   hasnt "TODO  create the release app"

echo
if [ "$FAILURES" -eq 0 ]; then echo "all bootstrap tests passed"; else echo "$FAILURES bootstrap test(s) failed"; exit 1; fi
