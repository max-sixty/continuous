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
# A failed read is not "nothing is open". Filing on it is how a repo ends up
# with two open trackers, and the reconcile does not clean this one up — it
# probes the ten numbers below the issue it just filed, and an already-open
# tracker is normally much older. Two of them scatter later rows across both,
# so no tracker carries the complete set the drain sweep needs. Skipping costs
# this one row on a transient failure, and the next failure records normally.
if ! EXISTING=$(run_issue_canonical "$LABEL" open "$TITLE"); then
  echo "::warning::Could not read this repo's ${LABEL} issues, so this run was not recorded on the outage tracker."
  exit 0
fi

if [ -n "$EXISTING" ]; then
  # The common path once a tracker is open — every failure after the first in
  # one incident appends through here — and it can 5xx like any other write.
  # Left bare it aborts under `set -e`, which drops the row without saying so:
  # the tracker then under-reports the outage, and a run stranded by it reads
  # as one that never happened. Warning costs the same single row a failed
  # read above costs, and the next failure records normally. The create below
  # keeps the opposite policy deliberately: with no tracker open there is no
  # other record of the outage, so a failed create has to redden the step.
  # The rate-limit caller guards both of its writes instead: a failed create
  # there must still reach the annotation that names what to close, where here
  # the create is the last statement and the red step is all that is left.
  if ! printf '%s\n' "$ROW" | gh issue comment "$EXISTING" -F -; then
    echo "::warning::Could not append this run's row to #${EXISTING}."
  fi
else
  printf '%s\n\n%s\n\n%s\n' \
    "The bot failed to process a request. This issue tracks failures until the underlying cause is resolved." \
    "$ROW" \
    "This issue was created automatically. Close it once the outage is resolved." \
    | run_issue_create_and_reconcile "$LABEL" "$TITLE" "$ROW" > /dev/null
fi
