#!/usr/bin/env bash
# Reruns a run's failed jobs, polls them to terminal, and prints each job's
# conclusion.
#
# Usage: rerun-failed-jobs.sh <run-id>
#
# The parent run's `.status` stays `in_progress` until every sibling job
# finishes — including unrelated long-running ones — and sibling check runs
# on the head SHA keep the commit rollup pending too, so the rerun's own jobs
# are the only reliable signal. They are found by attempt number: the script
# reads `run_attempt` before rerunning and waits for it to advance, which
# holds whether the rerun jobs are still queued or already finished — a
# status-based scan would read a fast rerun as "nothing re-queued" and a
# not-yet-registered one as the prior attempt's stale conclusions.
#
# Exit codes:
#   0  all rerun jobs terminal — one "<conclusion>\t<name>" line per job
#      (`completed` is not `success`: the follow-up turns on the conclusions)
#   1  no new attempt surfaced — the rerun did not take
#   3  jobs still running at the poll cap — UNVERIFIED

set -euo pipefail

RUN_ID="$1"
RUN_API="repos/$GITHUB_REPOSITORY/actions/runs/$RUN_ID"

BASE_ATTEMPT=$(gh api "$RUN_API" --jq '.run_attempt')
gh run rerun "$RUN_ID" --failed --repo "$GITHUB_REPOSITORY"

# The new attempt record takes a few seconds to surface.
ATTEMPT="$BASE_ATTEMPT"
for _ in $(seq 1 6); do
  sleep 5
  ATTEMPT=$(gh api "$RUN_API" --jq '.run_attempt')
  [ "$ATTEMPT" -gt "$BASE_ATTEMPT" ] && break
done
if [ "$ATTEMPT" -le "$BASE_ATTEMPT" ]; then
  echo "no new attempt surfaced after the rerun (still attempt $ATTEMPT) — the rerun did not take; UNVERIFIED"
  exit 1
fi

# `?filter=latest` returns each job's most recent attempt; jobs that were not
# re-run keep their prior attempt number, so this selects the rerun's jobs
# whatever their status.
JOB_IDS=$(gh api "$RUN_API/jobs?filter=latest" \
  --jq ".jobs[] | select(.run_attempt == $ATTEMPT) | .id")
if [ -z "$JOB_IDS" ]; then
  echo "attempt $ATTEMPT exists but lists no jobs yet — UNVERIFIED"
  exit 1
fi

pending_jobs() {
  local n=0 id s
  for id in $JOB_IDS; do
    s=$(gh api "repos/$GITHUB_REPOSITORY/actions/jobs/$id" --jq '.status')
    [ "$s" = "completed" ] || n=$((n + 1))
  done
  echo "$n"
}

for _ in $(seq 1 9); do
  if [ "$(pending_jobs)" -eq 0 ]; then
    for id in $JOB_IDS; do
      gh api "repos/$GITHUB_REPOSITORY/actions/jobs/$id" --jq '"\(.conclusion)\t\(.name)"'
    done
    exit 0
  fi
  sleep 60
done
echo "Rerun jobs still running after 9 minutes — UNVERIFIED"
exit 3
