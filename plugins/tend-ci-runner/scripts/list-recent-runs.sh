#!/usr/bin/env bash
# Lists recently completed Claude CI runs.
#
# Fetches a window of recent runs via one global `gh run list` call and
# filters client-side by workflow name prefix and updatedAt timestamp. The
# script keeps the past-1-hour completion cutoff: a run started 2h ago may
# have just finished, and a run started 50min ago may still be running, so
# we filter on updatedAt rather than createdAt.
#
# Why a global call instead of `--workflow X --created Y` per workflow:
# combining `--workflow` with secondary filters (`--created`, `--status`,
# `--branch`) is unreliable on gh 2.89.0. The same query can return [],
# return a stale subset, or return the correct set across consecutive calls
# — likely a workflow-scoped index lag in the GitHub Actions API. When the
# call returns [], this script silently produces zero runs and the analysis
# bypasses the entire window. Filtering client-side avoids the broken path.
#
# Environment variables:
#   TARGET_REPO - Query a different repo (default: current repo)
#
# Output: JSON array of {databaseId, conclusion, createdAt, updatedAt} objects.

set -euo pipefail

# Prevent gh from emitting ANSI color codes in non-TTY contexts.
export NO_COLOR=1

repo_args=()
if [ -n "${TARGET_REPO:-}" ]; then
  repo_args=(-R "$TARGET_REPO")
fi

# Filter by workflow name prefix. Multiple prefixes supported.
# Usage: ./list-recent-runs.sh [prefix ...]
if [ $# -eq 0 ]; then
  PREFIXES=("tend-")
else
  PREFIXES=("$@")
fi

COMPLETED_AFTER=$(date -d '1 hour ago' +%s)

prefixes_json=$(printf '%s\n' "${PREFIXES[@]}" | jq -R . | jq -s .)

# --limit 200 covers a few hours of activity even on busy repos. The 1-hour
# updatedAt cutoff bounds the output regardless.
gh run list \
  "${repo_args[@]}" \
  --limit 200 \
  --json databaseId,workflowName,conclusion,createdAt,updatedAt \
  | jq --argjson prefixes "$prefixes_json" --argjson cutoff "$COMPLETED_AFTER" '
    [ .[]
      | select(.conclusion != null and .conclusion != "")
      | select((.updatedAt | fromdateiso8601) >= $cutoff)
      | select(.workflowName as $n | $prefixes | any(. as $p | $n | startswith($p)))
      | {databaseId, conclusion, createdAt, updatedAt}
    ]
  '
