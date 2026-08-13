#!/usr/bin/env bash
# Reports token usage across recent tend CI runs.
#
# Downloads session log artifacts from GitHub Actions, extracts token
# counts from each run, and outputs a JSON report to stdout.
# A human-readable summary is printed to stderr.
#
# Reads the token-usage.json file from each run's session log artifact
# (produced by the "Token usage" step in each harness action).
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
  echo '{"runs":[],"totals":{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"turns":0,"cost_usd":0,"partial_runs":0}}'
  exit 0
fi

# Collect all completed runs across workflows.
#
# `gh run list` returns newest-first and silently stops at --limit, so a
# workflow busier than the limit drops the *oldest* runs in the window and the
# totals below under-report with nothing marking the shortfall. The limit is
# per workflow, not per report, so it only has to clear the busiest one; a
# repo's chattiest workflow can run several times an hour, and at this script's
# own documented 168 h default that already reaches the high hundreds. Capped
# at 1000 because that is the ceiling: the Actions runs endpoint stops
# paginating there whatever `total_count` says, so a larger constant is
# unreachable and would only make the guard below unable to fire.
#
# Warn rather than trust, in both directions. A count landing on the limit is
# the only symptom of truncation visible without re-querying `.total_count`,
# and a failed fetch is the same silent under-report at full strength: it drops
# every run of that workflow, and a length of 0 is not an exact-limit hit, so
# the truncation guard alone would pass straight over it.
RUN_LIMIT=1000
ALL_RUNS="[]"
for wf in "${WORKFLOWS[@]}"; do
  if ! runs=$(gh run list "${repo_args[@]}" --workflow "$wf" --created ">=$SINCE" --status completed \
    --json databaseId,conclusion,createdAt,name --limit "$RUN_LIMIT" 2>/dev/null); then
    echo >&2 "WARNING: 'gh run list' for '$wf' failed — its runs are absent from the totals below."
    runs="[]"
  elif [ "$(echo "$runs" | jq 'length')" -ge "$RUN_LIMIT" ]; then
    echo >&2 "WARNING: '$wf' returned $RUN_LIMIT runs, the Actions API's pagination ceiling — older runs in the window are unreachable and the totals below under-report it. Narrow HOURS to bring the window under the ceiling; raising RUN_LIMIT cannot help."
  fi
  ALL_RUNS=$(echo "$ALL_RUNS" "$runs" | jq -s 'add | unique_by(.databaseId)')
done

RUN_COUNT=$(echo "$ALL_RUNS" | jq 'length')
if [ "$RUN_COUNT" -eq 0 ]; then
  echo '{"runs":[],"totals":{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"turns":0,"cost_usd":0,"partial_runs":0}}'
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

  # Claude runs only: the `claude` action uploads `claude-session-logs-X`.
  # Runs on the codex harness upload `codex-session-logs-X` and are skipped
  # by the pattern below, so a codex-only repo reports zero.
  if ! gh run download "$RUN_ID" "${repo_args[@]}" \
      --pattern 'claude-session-logs*' \
      --dir "$RUNDIR" 2>/dev/null; then
    continue
  fi

  mapfile -t USAGE_FILES < <(find "$RUNDIR" -name "token-usage.json" -type f)
  if [ ${#USAGE_FILES[@]} -eq 0 ]; then
    continue
  fi

  # Aggregate across matrix jobs (each job produces its own token-usage.json).
  # `partial` marks a run whose counts were reconstructed from the session log
  # because it emitted no result event — its tokens are real but its cost is
  # unrecoverable, so the cost column under-counts by however many there are.
  USAGE=$(cat "${USAGE_FILES[@]}" | jq -s '{
    input_tokens: (map(.input_tokens) | add),
    output_tokens: (map(.output_tokens) | add),
    cache_creation_input_tokens: (map(.cache_creation_input_tokens) | add),
    cache_read_input_tokens: (map(.cache_read_input_tokens) | add),
    turns: (map(.turns) | add),
    cost_usd: (map(.cost_usd // 0) | add),
    partial: (map(.partial // false) | any)
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
    cost_usd: (map(.cost_usd) | add // 0 | . * 100 | round / 100),
    partial_runs: (map(select(.partial)) | length)
  }
}' "$ENTRIES" | tee "$WORKDIR/report.json"

# Human-readable summary to stderr
jq -r '
  def fmt:
    if . >= 1000000 then "\(. / 100000 | floor | . / 10)M"
    elif . >= 1000 then "\(. / 100 | floor | . / 10)K"
    else "\(.)" end;

  def usd: tostring | if test("\\.") then split(".") | "\(.[0]).\((.[1] + "00")[:2])" else . + ".00" end | "$" + .;

  # A partial run contributes tokens but no cost, so every cost it lands in —
  # its own row, its workflow row, the total — is a floor, not the spend. Mark
  # those cells with a trailing `+` so a reconstructed run never reads as free.
  def floor_marker: if . then "+" else "" end;

  "\n\(.runs | length) runs since '"$SINCE"'",
  "Totals: \(.totals.input_tokens | fmt) in, \(.totals.output_tokens | fmt) out, \(.totals.cache_creation_input_tokens | fmt) cache-create, \(.totals.cache_read_input_tokens | fmt) cache-read, \(.totals.cost_usd | usd)\(.totals.partial_runs > 0 | floor_marker) cost",
  "",
  (["WORKFLOW", "RUNS", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ", "COST"] | @tsv),
  (.runs | group_by(.workflow) | map({
    w: .[0].workflow,
    n: length,
    i: (map(.input_tokens) | add),
    o: (map(.output_tokens) | add),
    cc: (map(.cache_creation_input_tokens) | add),
    cr: (map(.cache_read_input_tokens) | add),
    cost: (map(.cost_usd) | add | . * 100 | round / 100),
    partial: (map(.partial // false) | any)
  }) | sort_by(.cr) | reverse | .[] |
    [.w, (.n | tostring), (.i | fmt), (.o | fmt), (.cc | fmt), (.cr | fmt), ((.cost | usd) + (.partial | floor_marker))] | @tsv),
  "",
  (["RUN", "WORKFLOW", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ", "COST", "TIME"] | @tsv),
  (.runs | sort_by(.created_at) | reverse | .[] |
    [(.run_id | tostring), .workflow, (.input_tokens | fmt), (.output_tokens | fmt), (.cache_creation_input_tokens | fmt), (.cache_read_input_tokens | fmt), ((.cost_usd | usd) + (.partial // false | floor_marker)), .created_at[:16]] | @tsv)
' "$WORKDIR/report.json" | column -t >&2

echo >&2 ""
# Printed outside the table's `column -t`, which would otherwise align a prose
# line into the table's columns.
PARTIAL_RUNS=$(jq -r '.totals.partial_runs' "$WORKDIR/report.json")
if [ "$PARTIAL_RUNS" -gt 0 ]; then
  echo >&2 "$PARTIAL_RUNS run(s) emitted no result event (typically cancelled): tokens counted, cost not recoverable. A '+' marks a cost that is a floor rather than the spend."
fi
echo >&2 "Cost at API list prices — a large multiple of the effective rate on Claude Code subscriptions."
