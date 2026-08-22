#!/usr/bin/env bash
# On a fork PR, pin every instruction file to the default branch before Codex
# starts, so a fork's copies aren't read as trusted repo guidance. The list
# and the reconcile are in lib/pin-instruction-paths.sh; the Claude harness
# runs the same ones from restore-sensitive-config.sh.
#
# Inputs (env, from Actions): GITHUB_EVENT_NAME, GITHUB_EVENT_PATH.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/pin-instruction-paths.sh
. "${SCRIPT_DIR}/lib/pin-instruction-paths.sh"

if [[ "$GITHUB_EVENT_NAME" != "pull_request_target" ]] ||
  [[ "$(jq -r '.pull_request.head.repo.fork // false' "$GITHUB_EVENT_PATH")" != true ]]; then
  exit 0
fi

DEFAULT_BRANCH=$(jq -r '.repository.default_branch' "$GITHUB_EVENT_PATH")
pin_to_base "origin/${DEFAULT_BRANCH}" "${INSTRUCTION_PATHSPECS[@]}"
