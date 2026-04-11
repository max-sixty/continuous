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
# Usage: ./token-report.sh [HOURS] [PREFIX ...]
#   HOURS: lookback period in hours (default: 168 = 7 days)
#   PREFIX: additional workflow name prefixes to include (default: tend-)
#
# Output (stdout): JSON — { runs: [...], totals: {...} }
# Output (stderr): human-readable summary table
#
# Environment:
#   TARGET_REPO - query a different repo (default: current repo)
#
# Requires: gh, jq

set -euo pipefail
# Disable gh's colored JSON output. NO_COLOR=1 alone is insufficient when the
# environment sets CLICOLOR_FORCE=1 (e.g. PRQL/prql's tend-setup action sets
# it in $GITHUB_ENV to force cargo/clippy colors), because gh treats
# CLICOLOR_FORCE as higher priority than NO_COLOR — resulting in ANSI codes
# in --json output that break downstream jq parsing.
export NO_COLOR=1
export CLICOLOR_FORCE=0

HOURS=${1:-168}
shift 2>/dev/null || true
EXTRA_PREFIXES=("$@")

SINCE=$(date -u -d "$HOURS hours ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-"${HOURS}"H +%Y-%m-%dT%H:%M:%SZ)

repo_args=()
if [ -n "${TARGET_REPO:-}" ]; then
  repo_args=(-R "$TARGET_REPO")
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# Discover tend workflows (tend-* by default, plus any extra prefixes)
PREFIXES=("tend-" "${EXTRA_PREFIXES[@]}")
WORKFLOWS=()
for prefix in "${PREFIXES[@]}"; do
  mapfile -t matches < <(
    gh workflow list "${repo_args[@]}" --json name --jq ".[].name | select(startswith(\"$prefix\"))"
  )
  WORKFLOWS+=("${matches[@]}")
done

if [ ${#WORKFLOWS[@]} -eq 0 ]; then
  echo '{"runs":[],"totals":{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"turns":0,"cost_usd":0}}'
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
  echo '{"runs":[],"totals":{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"turns":0,"cost_usd":0}}'
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

  mapfile -t USAGE_FILES < <(find "$RUNDIR" -name "token-usage.json" -type f)
  if [ ${#USAGE_FILES[@]} -eq 0 ]; then
    continue
  fi

  # Aggregate across matrix jobs (each job produces its own token-usage.json)
  USAGE=$(cat "${USAGE_FILES[@]}" | jq -s '{
    input_tokens: (map(.input_tokens) | add),
    output_tokens: (map(.output_tokens) | add),
    cache_creation_input_tokens: (map(.cache_creation_input_tokens) | add),
    cache_read_input_tokens: (map(.cache_read_input_tokens) | add),
    turns: (map(.turns) | add),
    cost_usd: (map(.cost_usd // 0) | add)
  }')

  jq -c --argjson usage "$USAGE" '
    . + $usage + {run_id: .databaseId, workflow: .name, created_at: .createdAt} |
    del(.databaseId, .name, .createdAt)' <<< "$row" >> "$ENTRIES"

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
    turns: (map(.turns) | add // 0),
    cost_usd: (map(.cost_usd) | add // 0 | . * 100 | round / 100)
  }
}' "$ENTRIES" | tee "$WORKDIR/report.json"

# Human-readable summary to stderr
jq -r '
  def fmt:
    if . >= 1000000 then "\(. / 100000 | floor | . / 10)M"
    elif . >= 1000 then "\(. / 100 | floor | . / 10)K"
    else "\(.)" end;

  def usd: tostring | if test("\\.") then split(".") | "\(.[0]).\((.[1] + "00")[:2])" else . + ".00" end | "$" + .;

  "\n\(.runs | length) runs since '"$SINCE"'",
  "Totals: \(.totals.input_tokens | fmt) in, \(.totals.output_tokens | fmt) out, \(.totals.cache_creation_input_tokens | fmt) cache-create, \(.totals.cache_read_input_tokens | fmt) cache-read, \(.totals.cost_usd | usd) cost",
  "",
  (["WORKFLOW", "RUNS", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ", "COST"] | @tsv),
  (.runs | group_by(.workflow) | map({
    w: .[0].workflow,
    n: length,
    i: (map(.input_tokens) | add),
    o: (map(.output_tokens) | add),
    cc: (map(.cache_creation_input_tokens) | add),
    cr: (map(.cache_read_input_tokens) | add),
    cost: (map(.cost_usd) | add | . * 100 | round / 100)
  }) | sort_by(.cr) | reverse | .[] |
    [.w, (.n | tostring), (.i | fmt), (.o | fmt), (.cc | fmt), (.cr | fmt), (.cost | usd)] | @tsv),
  "",
  (["RUN", "WORKFLOW", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ", "COST", "TIME"] | @tsv),
  (.runs | sort_by(.created_at) | reverse | .[] |
    [(.run_id | tostring), .workflow, (.input_tokens | fmt), (.output_tokens | fmt), (.cache_creation_input_tokens | fmt), (.cache_read_input_tokens | fmt), (.cost_usd | usd), .created_at[:16]] | @tsv)
' "$WORKDIR/report.json" | column -t >&2

echo >&2 ""
echo >&2 "Cost at API list prices — a large multiple of the effective rate on Claude Code subscriptions."
