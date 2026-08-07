#!/usr/bin/env bash
# File or append to a `tend-outage` issue when a run fails, so outages are
# tracked until resolved. Shared verbatim by both harness actions; the
# caller gates it on the agent step having failed.
#
# Just records the run link. Error annotations and logs are not reliably
# available while the job is in_progress, so the nightly skill enriches these
# issues after the fact, when the run has completed and the APIs return stable
# data.
#
# A closed outage issue is left closed and a fresh one filed: closing it means
# the outage was resolved, and reopening would fold the next incident into a
# stale record. The rate-limit issue takes the opposite policy, for reasons in
# lib/run-issue.sh.
#
# Inputs (env): GITHUB_TOKEN (for gh), GITHUB_SERVER_URL, GITHUB_REPOSITORY,
# GITHUB_RUN_ID, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH (from Actions).
set -eo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/run-issue.sh
. "${SCRIPT_DIR}/lib/run-issue.sh"

LABEL="tend-outage"
TITLE="Bot temporarily unavailable"

ROW=$(run_issue_row)

run_issue_ensure_label "$LABEL" "Tracks bot outage incidents" "d93f0b"

# Jittered backoff before the check-then-act narrows the race window when a
# matrix workflow's legs fail at near-identical times (e.g. model-API 5xx
# responses exhausting the retry budget across every leg within a few
# seconds). Without this, every leg reads $EXISTING as empty in parallel and
# each files its own outage issue.
sleep $((RANDOM % 30))
EXISTING=$(run_issue_canonical "$LABEL" open "$TITLE")

if [ -n "$EXISTING" ]; then
  printf '%s\n' "$ROW" | gh issue comment "$EXISTING" -F -
else
  printf '%s\n\n%s\n\n%s\n' \
    "The bot failed to process a request. This issue tracks failures until the underlying cause is resolved." \
    "$ROW" \
    "This issue was created automatically. Close it once the outage is resolved." \
    | run_issue_create_and_reconcile "$LABEL" "$TITLE" "$ROW" > /dev/null
fi
