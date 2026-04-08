#!/usr/bin/env bash
# Reports token usage across recent tend CI runs.
#
# Downloads session log artifacts from GitHub Actions, extracts token
# counts from each run, and outputs a JSON report to stdout.
# A human-readable summary is printed to stderr.
#
# Reads the token-usage.json file from each run's session log artifact
# (produced by the "Token usage" step in action.yaml).
#
# Usage: ./token-report.sh [HOURS]
#   HOURS: lookback period in hours (default: 168 = 7 days)
#
# Output (stdout): JSON — { runs: [...], totals: {...} }
# Output (stderr): human-readable summary table
#
# Environment:
#   TARGET_REPO - query a different repo (default: current repo)
#
# Requires: gh, jq, GNU coreutils (date -d)

set -euo pipefail
export NO_COLOR=1

HOURS=${1:-168}
SINCE=$(date -u -d "$HOURS hours ago" +%Y-%m-%dT%H:%M:%SZ)

repo_args=()
if [ -n "${TARGET_REPO:-}" ]; then
  repo_args=(-R "$TARGET_REPO")
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# Discover tend workflows
mapfile -t WORKFLOWS < <(
  gh workflow list "${repo_args[@]}" --json name --jq '.[].name | select(startswith("tend-"))'
)

if [ ${#WORKFLOWS[@]} -eq 0 ]; then
  echo '{"runs":[],"totals":{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"turns":0}}'
  exit 0
fi

# Collect all completed runs across workflows
ALL_RUNS="[]"
for wf in "${WORKFLOWS[@]}"; do
  runs=$(gh run list "${repo_args[@]}" --workflow "$wf" --created ">=$SINCE" --status completed \
    --json databaseId,conclusion,createdAt,name --limit 100 2>/dev/null || echo "[]")
  ALL_RUNS=$(echo "$ALL_RUNS" "$runs" | jq -s 'add | unique_by(.databaseId)')
done

RUN_COUNT=$(echo "$ALL_RUNS" | jq 'length')
if [ "$RUN_COUNT" -eq 0 ]; then
  echo '{"runs":[],"totals":{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"turns":0}}'
  exit 0
fi

echo >&2 "Downloading artifacts for $RUN_COUNT runs..."

ENTRIES="$WORKDIR/entries.jsonl"
touch "$ENTRIES"

mapfile -t ROWS < <(echo "$ALL_RUNS" | jq -c '.[]')
for row in "${ROWS[@]}"; do
  RUN_ID=$(echo "$row" | jq -r '.databaseId')
  RUNDIR="$WORKDIR/$RUN_ID"
  mkdir -p "$RUNDIR"

  if ! gh run download "$RUN_ID" "${repo_args[@]}" --pattern 'claude-session-logs*' --dir "$RUNDIR" 2>/dev/null; then
    continue
  fi

  USAGE_FILE=$(find "$RUNDIR" -name "token-usage.json" -type f | head -1)
  if [ -z "$USAGE_FILE" ]; then
    continue
  fi

  jq -c --argjson usage "$(cat "$USAGE_FILE")" \
    '. + $usage + {run_id: .databaseId, workflow: .name, created_at: .createdAt} | del(.databaseId, .name, .createdAt)' \
    <<< "$row" >> "$ENTRIES"

  rm -rf "$RUNDIR"
done

# Build final output: runs array + totals
jq -s '{
  runs: .,
  totals: {
    input_tokens: (map(.input_tokens) | add // 0),
    output_tokens: (map(.output_tokens) | add // 0),
    cache_creation_input_tokens: (map(.cache_creation_input_tokens) | add // 0),
    cache_read_input_tokens: (map(.cache_read_input_tokens) | add // 0),
    turns: (map(.turns) | add // 0)
  }
}' "$ENTRIES" | tee "$WORKDIR/report.json"

# Human-readable summary to stderr
jq -r '
  def fmt:
    if . >= 1000000 then "\(. / 100000 | floor | . / 10)M"
    elif . >= 1000 then "\(. / 100 | floor | . / 10)K"
    else "\(.)" end;

  "\n\(.runs | length) runs since '"$SINCE"'",
  "Totals: \(.totals.input_tokens | fmt) in, \(.totals.output_tokens | fmt) out, \(.totals.cache_creation_input_tokens | fmt) cache-create, \(.totals.cache_read_input_tokens | fmt) cache-read",
  "",
  (["WORKFLOW", "RUNS", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ"] | @tsv),
  (.runs | group_by(.workflow) | map({
    w: .[0].workflow,
    n: length,
    i: (map(.input_tokens) | add),
    o: (map(.output_tokens) | add),
    cc: (map(.cache_creation_input_tokens) | add),
    cr: (map(.cache_read_input_tokens) | add)
  }) | sort_by(.cr) | reverse | .[] |
    [.w, (.n | tostring), (.i | fmt), (.o | fmt), (.cc | fmt), (.cr | fmt)] | @tsv)
' "$WORKDIR/report.json" | column -t >&2
