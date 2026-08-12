#!/usr/bin/env bash
# Polls the jobs of a rerun until every one is terminal, then prints each
# job's conclusion.
#
# Usage: poll-rerun-jobs.sh <run-id>   (after `gh run rerun <run-id> --failed`)
#
# The parent run's `.status` stays `in_progress` until every sibling job
# finishes — including unrelated long-running ones — and sibling check runs
# on the head SHA keep the commit rollup pending too, so the specific job IDs
# are the only reliable signal.
#
# Exit codes:
#   0  all rerun jobs terminal — one "<conclusion>\t<name>" line per job
#      (`completed` is not `success`: the follow-up turns on the conclusions)
#   1  no rerun attempt surfaced — the jobs were not re-queued
#   3  jobs still running at the poll cap — UNVERIFIED

set -euo pipefail

RUN_ID="$1"

# New attempt records take a few seconds to surface; without this sleep the
# query below can see only the prior attempt's `failure` rows.
sleep 10

# `?filter=latest` returns each job's most recent attempt.
JOB_IDS=$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/$RUN_ID/jobs?filter=latest" \
  --jq '.jobs[] | select(.status != "completed") | .id')
if [ -z "$JOB_IDS" ]; then
  echo "No rerun attempt surfaced — jobs were not re-queued"
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
