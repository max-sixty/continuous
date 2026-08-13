#!/usr/bin/env bash
# Lists recently completed tend CI runs as a JSON array of
# {databaseId, conclusion, createdAt, updatedAt} objects.
#
# The completion window resumes where the previous successful run of the
# calling workflow left off: the floor is that run's start time. A run's own
# window always opens at or before its own start, so consecutive windows
# overlap by a few minutes and never gap — the caller dedups against its
# evidence log, so overlap is cheap, where a gap is silently unanalyzed.
# Scheduler delay, dropped ticks, cadence changes, and manual dispatches all
# need no special handling: whatever run last succeeded is the anchor. A
# *failed* run analyzed nothing, so it never anchors; its window rides along
# to the next success. The floor is clamped at 6h so an outage can't grow
# the window unboundedly; when the clamp bites, or no successful run exists,
# a WARNING on stderr tells the caller to record a coverage gap rather than
# an all-clear. Outside GitHub Actions (no GITHUB_WORKFLOW), the window is
# simply the past hour.
#
# Runs are fetched by *creation* time with a 2h cushion below the floor,
# then filtered on completion (updatedAt): `gh run list --created` filters
# by start time, and a run started before the floor may have finished
# inside the window.
#
# A transient API failure fails the script loudly (`set -e`); the caller
# treats that as "window not analyzed", never as an all-clear, and the next
# tick's floor reaches back past the lost window.
#
# Environment variables:
#   TARGET_REPO - Query a different repo's runs (default: current repo).
#                 The window anchor always comes from the current repo,
#                 where the calling workflow runs.
#
# Usage: ./list-recent-runs.sh [prefix ...]
#   Workflow-name prefixes to include (default: "tend-").

set -euo pipefail

# Prevent gh from emitting ANSI color codes in non-TTY contexts.
export NO_COLOR=1

repo_args=()
if [ -n "${TARGET_REPO:-}" ]; then
  repo_args=(-R "$TARGET_REPO")
fi

if [ $# -eq 0 ]; then
  PREFIXES=("tend-")
else
  PREFIXES=("$@")
fi

wf_json=$(gh workflow list "${repo_args[@]}" --json name)

WORKFLOWS=()
for prefix in "${PREFIXES[@]}"; do
  mapfile -t matches < <(printf '%s' "$wf_json" | jq -r ".[].name | select(startswith(\"$prefix\"))")
  WORKFLOWS+=("${matches[@]}")
done

now=$(date -u +%s)
floor_cap=$((now - 21600))
if [ -n "${GITHUB_WORKFLOW:-}" ]; then
  # Exclude this run itself: a re-run attempt of it can already read as a
  # completed success, and anchoring on it would collapse the window to zero.
  # The anchor comes from the repo the workflow runs in — $GITHUB_REPOSITORY,
  # named explicitly so the query doesn't lean on cwd remote detection —
  # never from TARGET_REPO.
  prev_start=$(gh run list --repo "$GITHUB_REPOSITORY" --workflow "$GITHUB_WORKFLOW" \
    --status success --limit 5 --json databaseId,createdAt \
    --jq "[.[] | select(.databaseId != (${GITHUB_RUN_ID:-0}))] | .[0].createdAt // empty")
  if [ -n "$prev_start" ]; then
    COMPLETED_AFTER=$(date -u -d "$prev_start" +%s)
    if [ "$COMPLETED_AFTER" -lt "$floor_cap" ]; then
      echo "WARNING: the last successful '$GITHUB_WORKFLOW' run started $prev_start, more than 6h back. Window floored at $(date -u -d "@$floor_cap" +%Y-%m-%dT%H:%M:%SZ); runs that completed before it are NOT in this list. Record a coverage gap, not an all-clear." >&2
      COMPLETED_AFTER=$floor_cap
    fi
  else
    echo "WARNING: no successful '$GITHUB_WORKFLOW' run found. Window floored at $(date -u -d "@$floor_cap" +%Y-%m-%dT%H:%M:%SZ); anything earlier is NOT in this list. Record a coverage gap, not an all-clear." >&2
    COMPLETED_AFTER=$floor_cap
  fi
else
  COMPLETED_AFTER=$((now - 3600))
fi

CREATED_SINCE=$(date -u -d "@$((COMPLETED_AFTER - 7200))" +%Y-%m-%dT%H:%M:%S)

# `gh run list` returns newest-first, so a workflow with more runs in the
# window than the limit silently drops the *oldest* — exactly the runs a
# widened window reached back for. Warn rather than fail at the cap: the rows
# in hand are still worth analyzing; the caller just can't read the list as
# complete.
RUN_LIMIT=200

all_runs="[]"
for wf in "${WORKFLOWS[@]}"; do
  runs=$(gh run list \
    "${repo_args[@]}" \
    --workflow "${wf}" \
    --created ">=${CREATED_SINCE}" \
    --json databaseId,conclusion,createdAt,updatedAt \
    --limit "$RUN_LIMIT")
  if [ "$(printf '%s' "$runs" | jq 'length')" -ge "$RUN_LIMIT" ]; then
    echo "WARNING: '$wf' returned $RUN_LIMIT runs, the fetch limit — older runs in this window are likely missing from the list. Record a coverage gap, not an all-clear." >&2
  fi
  all_runs=$(echo "$all_runs" "$runs" | jq -s 'add')
done

# Filter: drop in-progress (empty conclusion), keep only recently finished.
# unique_by: overlapping prefixes can match one workflow twice, which would
# double-count its runs.
echo "$all_runs" | jq --argjson cutoff "$COMPLETED_AFTER" '
  [ .[]
    | select(.conclusion != null and .conclusion != "")
    | select((.updatedAt | fromdateiso8601) >= $cutoff)
  ] | unique_by(.databaseId)
'
