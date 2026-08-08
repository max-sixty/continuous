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
  # pytest exits 5 for "no tests collected" — what a -k aimed at one suite looks
  # like from the others. Only a filtered run gets to treat that as a pass.
  if [ "$rc" -eq 5 ] && [ ${#pytest_args[@]} -gt 0 ]; then rc=0; fi
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
  # Install only when the tree is missing, and with `npm ci` rather than
  # `npm install` — an older local npm reruns resolution and rewrites
  # package-lock.json, leaving churn in the diff that has nothing to do with the
  # change under test.
  if [ ! -d worker/node_modules ]; then
    suite worker npm ci --prefer-offline --no-audit --no-fund
  fi
  suite worker npm run typecheck
  suite worker npm test
fi

if [ ${#failed[@]} -gt 0 ]; then
  printf '\nfailed: %s\n' "${failed[*]}" >&2
  exit 1
fi
