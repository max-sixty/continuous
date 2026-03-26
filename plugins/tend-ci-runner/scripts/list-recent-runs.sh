#!/usr/bin/env bash
# Lists recently completed Claude CI runs.
#
# Fetches runs started in the past 3 hours, then filters to only those that
# are completed and whose updatedAt is within the past hour. This two-step
# approach is needed because `gh run list --created` filters by *start* time,
# not *end* time — a run started 2h ago may have just finished, and a run
# started 50min ago may still be running. See #1301 for details.
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

# Dynamically discover workflows by prefix. Multiple prefixes supported.
# Usage: ./list-recent-runs.sh [prefix ...]
if [ $# -eq 0 ]; then
  PREFIXES=("tend-")
else
  PREFIXES=("$@")
fi

WORKFLOWS=()
for prefix in "${PREFIXES[@]}"; do
  mapfile -t matches < <(gh workflow list "${repo_args[@]}" --json name --jq ".[].name | select(startswith(\"$prefix\"))")
  WORKFLOWS+=("${matches[@]}")
done

CREATED_SINCE=$(date -d '3 hours ago' +%Y-%m-%dT%H:%M:%S)
COMPLETED_AFTER=$(date -d '1 hour ago' +%s)

all_runs="[]"

for wf in "${WORKFLOWS[@]}"; do
  runs=$(gh run list \
    "${repo_args[@]}" \
    --workflow "${wf}" \
    --created ">=${CREATED_SINCE}" \
    --json databaseId,conclusion,createdAt,updatedAt \
    --limit 50 2>/dev/null || echo "[]")
  all_runs=$(echo "$all_runs" "$runs" | jq -s 'add')
done

# Filter: drop in-progress (empty conclusion), keep only recently finished
echo "$all_runs" | jq --argjson cutoff "$COMPLETED_AFTER" '
  [ .[]
    | select(.conclusion != null and .conclusion != "")
    | select((.updatedAt | fromdateiso8601) >= $cutoff)
  ]
'
