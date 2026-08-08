#!/usr/bin/env bash
# Every test suite in the repo, mirroring the test jobs in
# .github/workflows/ci.yaml. `wt test` runs this (see .config/wt.toml), so one
# command covers generator/, proxy/, the install-tend scripts, and worker/.
#
# Arguments are forwarded to the pytest suites (`wt test -k render`); a filtered
# run skips worker/, whose vitest CLI takes different flags. Every suite runs
# even if an earlier one fails, and the failures are listed at the end.
set -o pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

failed=()

# suite <dir> <cmd>...
suite() {
  local dir=$1 rc=0
  shift
  printf '\n==> %s: %s\n' "$dir" "$*"
  (cd "$dir" && "$@") || rc=$?
  # A filtered run gets slack: pytest exits 5 for "no tests collected" and 4 for
  # a path that only exists in another suite. generator/ stays strict, so a bad
  # flag — 4 everywhere — still fails the run.
  if [ ${#pytest_args[@]} -gt 0 ] &&
    { [ "$rc" -eq 5 ] || { [ "$rc" -eq 4 ] && [ "$dir" != generator ]; }; }; then
    rc=0
  fi
  if [ "$rc" -ne 0 ]; then failed+=("$dir"); fi
}

pytest_args=("$@")

suite generator uv run pytest "${pytest_args[@]}"

# The proxy addon isn't part of the generator package, and it imports
# mitmproxy.test, so it runs standalone against the version production runs
# rather than whatever mitmproxy is latest.
if mitmproxy_version=$(yq -e '.inputs.mitmproxy_version.default' claude/action.yaml); then
  suite proxy uv run --no-project --with pytest \
    --with "mitmproxy==$mitmproxy_version" pytest "${pytest_args[@]}"
else
  echo "==> proxy: cannot read mitmproxy_version from claude/action.yaml (yq installed?)" >&2
  failed+=(proxy)
fi

suite plugins/install-tend/skills/install-tend/scripts \
  uv run --no-project --with pytest pytest "${pytest_args[@]}"

if [ ${#pytest_args[@]} -eq 0 ]; then
  # Install when the tree is missing or older than the lockfile (`npm ci` writes
  # node_modules/.package-lock.json), and with `npm ci` rather than `npm install`
  # — an older local npm reruns resolution and rewrites package-lock.json,
  # leaving churn in the diff that has nothing to do with the change under test.
  if [ ! -d worker/node_modules ] ||
    [ worker/package-lock.json -nt worker/node_modules/.package-lock.json ]; then
    suite worker npm ci --prefer-offline --no-audit --no-fund
  fi
  suite worker npm run typecheck
  suite worker npm test
fi

if [ ${#failed[@]} -gt 0 ]; then
  printf '\nfailed: %s\n' "${failed[*]}" >&2
  exit 1
fi
