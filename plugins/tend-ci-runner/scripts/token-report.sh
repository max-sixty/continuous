#!/usr/bin/env bash
# Reports token spend across recent tend CI runs.
#
# Output (stdout): JSON — { runs: [...], totals: {...} }
# Output (stderr): a human-readable summary
#
# Usage: ./token-report.sh [HOURS] [PREFIX ...]
#   HOURS: lookback period in hours (default: 168 = 7 days)
#   PREFIX: additional workflow name prefixes to include (default: tend-)
#
# Environment:
#   TARGET_REPO - query a different repo (default: current repo)
#
# Requires: gh, jq
#
# Shape: shell talks to GitHub, jq does the arithmetic, and one canonical form
# joins them. The download loop writes one JSON line per *job* — the
# `token-usage.json` the "Token usage" step wrote, plus the run's row from
# `gh run list` — and everything downstream reads that stream. So there are
# three jq programs and no shell arithmetic: the loop's one-line stamp, the
# report (jobs to runs to totals), and the summary.
#
# The summary's rollup tables sort by cost. A cached input token is priced far
# below an output token, so ranking by token volume, which cache reads
# dominate, is not ranking by spend. The per-run table stays newest-first.

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

# One JSON object per job. An empty file is a report of no runs, which the two
# programs below produce from it without a special case.
JOBS="$WORKDIR/jobs.jsonl"
: > "$JOBS"

# Discover tend workflows (tend-* by default, plus any extra prefixes)
PREFIXES=("tend-" "${EXTRA_PREFIXES[@]}")
WORKFLOWS=()
for prefix in "${PREFIXES[@]}"; do
  mapfile -t matches < <(
    gh workflow list "${repo_args[@]}" --json name --jq ".[].name | select(startswith(\"$prefix\"))"
  )
  WORKFLOWS+=("${matches[@]}")
done

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
    --json databaseId,createdAt,name --limit "$RUN_LIMIT" 2>/dev/null); then
    echo >&2 "WARNING: 'gh run list' for '$wf' failed — its runs are absent from the totals below."
    runs="[]"
  elif [ "$(echo "$runs" | jq 'length')" -ge "$RUN_LIMIT" ]; then
    echo >&2 "WARNING: '$wf' returned $RUN_LIMIT runs, the Actions API's pagination ceiling — older runs in the window are unreachable and the totals below under-report it. Narrow HOURS to bring the window under the ceiling; raising RUN_LIMIT cannot help."
  fi
  ALL_RUNS=$(echo "$ALL_RUNS" "$runs" | jq -s 'add | unique_by(.databaseId)')
done

# The run listing is the source for these three fields and the record the job
# uploaded is the source for the rest, so they are renamed once here and
# nothing downstream carries `gh`'s spelling.
mapfile -t ROWS < <(
  echo "$ALL_RUNS" | jq -c '.[] | {run_id: .databaseId, workflow: .name, created_at: .createdAt}'
)
echo >&2 "Downloading artifacts for ${#ROWS[@]} runs..."

# Runs that reach the report with nothing, counted rather than dropped
# silently: a codex-harness repo would otherwise read a report of zero runs
# with no line saying why, and the same silence covers a torn upload.
SKIPPED=0

for row in "${ROWS[@]}"; do
  RUN_ID=$(echo "$row" | jq -r '.run_id')
  RUNDIR="$WORKDIR/$RUN_ID"
  mkdir -p "$RUNDIR"

  # Claude runs only: the `claude` action uploads `claude-session-logs-X`, and
  # the pattern below leaves the codex harness's `codex-session-logs-X` alone,
  # so a codex-only repo reports zero runs and says so.
  if gh run download "$RUN_ID" "${repo_args[@]}" \
      --pattern 'claude-session-logs*' --dir "$RUNDIR" 2>/dev/null; then
    mapfile -t USAGE_FILES < <(find "$RUNDIR" -name "token-usage.json" -type f)
  else
    USAGE_FILES=()
  fi

  # Stamp the run onto each of its jobs — a matrix run uploads one file per leg
  # — and hold the result aside until the whole run parses. jq emits as it
  # reads, so a torn file fails only after its predecessors are out, and
  # appending those would count a run twice. An unreadable or empty artifact
  # therefore costs its own run and no other; without the guard `set -e` would
  # end the script here and take every other run's data with it. jq's own
  # error goes to stderr unsuppressed, which is what names the torn file.
  if [ ${#USAGE_FILES[@]} -gt 0 ] &&
    jq -c --argjson run "$row" '. + $run' "${USAGE_FILES[@]}" > "$RUNDIR/jobs.jsonl" &&
    [ -s "$RUNDIR/jobs.jsonl" ]; then
    cat "$RUNDIR/jobs.jsonl" >> "$JOBS"
  else
    SKIPPED=$((SKIPPED + 1))
  fi

  rm -rf "$RUNDIR"
done

# Jobs to runs to totals. `partial` marks a run whose counts were reconstructed
# from the session log because it emitted no result event: its tokens are real
# and its cost is not recoverable, so every cost it lands in is a floor.
jq -s --argjson skipped "$SKIPPED" '
  def sum(f): map(f) | add // 0;
  # The identity fields are per-run, so the first leg carrying one speaks for
  # the run; `empty` drops the nulls an event with no thread, or an artifact
  # written before the record carried these, leaves behind.
  def pick(f): map(f // empty) | first;

  def run_entry:
    {
      run_id: .[0].run_id,
      workflow: .[0].workflow,
      created_at: .[0].created_at,
      repo: pick(.repo),
      event: pick(.event),
      number: pick(.number),
      head_sha: pick(.head_sha),
      input_tokens: sum(.input_tokens),
      output_tokens: sum(.output_tokens),
      cache_creation_input_tokens: sum(.cache_creation_input_tokens),
      cache_read_input_tokens: sum(.cache_read_input_tokens),
      turns: sum(.turns),
      cost_usd: sum(.cost_usd),
      partial: (map(.partial // false) | any)
    }
    # What the run was about, as one cell: the PR or issue it worked on, else
    # the commit, else `?` for a record written before these fields existed or
    # one whose event named neither. Computed once, here, so every reader of
    # the report groups by the same key.
    | .subject = (if .number then "#\(.number)" elif .head_sha then .head_sha else "?" end);

  (group_by(.run_id) | map(run_entry) | sort_by(.created_at) | reverse) as $runs
  | {
      runs: $runs,
      totals: ($runs | {
        input_tokens: sum(.input_tokens),
        output_tokens: sum(.output_tokens),
        cache_creation_input_tokens: sum(.cache_creation_input_tokens),
        cache_read_input_tokens: sum(.cache_read_input_tokens),
        turns: sum(.turns),
        cost_usd: (sum(.cost_usd) | . * 100 | round / 100),
        partial_runs: (map(select(.partial)) | length),
        skipped_runs: $skipped
      })
    }
' "$JOBS" | tee "$WORKDIR/report.json"

jq -r --arg since "$SINCE" '
  # How many of the costliest subjects the summary shows.
  def top: 20;

  def fmt:
    if . >= 1000000 then "\(. / 100000 | floor | . / 10)M"
    elif . >= 1000 then "\(. / 100 | floor | . / 10)K"
    else "\(.)" end;

  # Rounds here rather than in the report, so a matrix run whose per-leg costs
  # sum to 28.169999999999995 reads the same in its own row as in its total.
  def usd:
    (. * 100 | round / 100) | tostring
    | if test("\\.") then split(".") | "\(.[0]).\((.[1] + "00")[:2])" else . + ".00" end
    | "$" + .;

  # A cost a partial run lands in is a floor, not the spend. Marked so a
  # reconstructed run never reads as free.
  def floor_marker: if . then "+" else "" end;

  # A full sha is the join key; the column has room for a prefix.
  def short: .[:12];

  # Rows are padded here rather than piped through `column -t`, which splits on
  # whitespace: a workflow name with a space in it would shift every column
  # right, and one pass over the whole summary would align the prose lines into
  # the tables and eat the blank lines between them.
  def pad($w): . + ((" " * ($w - length)) // "");
  def table($rows):
    ($rows | transpose | map(map(length) | max)) as $w
    | $rows
    | map(. as $row
      | [range(0; $row | length) as $i | $row[$i] | pad($w[$i])]
      | join("  ") | sub(" +$"; ""));

  def rollup: {
    n: length,
    cost: (map(.cost_usd) | add // 0),
    partial: (map(.partial) | any),
    i: (map(.input_tokens) | add // 0),
    o: (map(.output_tokens) | add // 0),
    cc: (map(.cache_creation_input_tokens) | add // 0),
    cr: (map(.cache_read_input_tokens) | add // 0)
  };
  def cost_cell: (.cost | usd) + (.partial | floor_marker);
  def by_cost: sort_by(.cost) | reverse;
  def subjects:
    group_by(.subject)
    | map({k: (.[0].subject | short), wf: (map(.workflow) | unique | join(","))} + rollup);

  .totals as $t
  | .runs as $runs
  | [
      "",
      "\($runs | length) runs since \($since)",
      "Total cost: \($t.cost_usd | usd)\($t.partial_runs > 0 | floor_marker)"
        + (if $t.partial_runs > 0
           then " (\($t.partial_runs) of \($runs | length) runs cost-unknown)" else "" end),
      "Tokens: \($t.input_tokens | fmt) in, \($t.output_tokens | fmt) out, \($t.cache_creation_input_tokens | fmt) cache-create, \($t.cache_read_input_tokens | fmt) cache-read",
      ""
    ]
  + table(
      [["WORKFLOW", "RUNS", "COST", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ"]]
      + ($runs | group_by(.workflow) | map({k: .[0].workflow} + rollup) | by_cost
         | map([.k, (.n | tostring), cost_cell, (.i | fmt), (.o | fmt), (.cc | fmt), (.cr | fmt)]))
    ) + [""]
  # Repeat work on one subject is what this table exists to show: a PR reviewed
  # over and over, or two agents racing on one commit, is one row with a high
  # RUNS count.
  + table(
      [["SUBJECT", "RUNS", "COST", "WORKFLOWS", "CACHE-READ"]]
      + ($runs | subjects | by_cost | .[:top]
         | map([.k, (.n | tostring), cost_cell, .wf, (.cr | fmt)]))
    ) + [""]
  # A run with no result event is booked at $0, because a floor is the only
  # honest number for it — so the cost sort buries it and the cut above drops
  # it, and a subject whose runs were all cancelled disappears from a report
  # about where the tokens went. Its tokens are real, so it gets a table of its
  # own, ranked by the one number it has, and uncapped: these are a small
  # fraction of runs, and a fleet where they are not is itself the finding.
  # Pricing them instead would mean carrying a price table, which is the thing
  # the record deliberately does not do.
  + (if $t.partial_runs > 0 then
      table(
        [["COST-UNKNOWN", "RUNS", "CACHE-READ", "OUTPUT", "WORKFLOWS"]]
        + ($runs | map(select(.partial)) | subjects | sort_by(.cr) | reverse
           | map([.k, (.n | tostring), (.cr | fmt), (.o | fmt), .wf]))
      ) + [""]
     else [] end)
  + table(
      [["RUN", "WORKFLOW", "SUBJECT", "COST", "INPUT", "OUTPUT", "CACHE-CREATE", "CACHE-READ", "TIME"]]
      + ($runs | map([(.run_id | tostring), .workflow, (.subject | short),
                      ((.cost_usd | usd) + (.partial | floor_marker)),
                      (.input_tokens | fmt), (.output_tokens | fmt),
                      (.cache_creation_input_tokens | fmt), (.cache_read_input_tokens | fmt),
                      .created_at[:16]]))
    ) + [""]
  + (($runs | map(.subject) | unique | length) as $n
     | if $n > top
       then ["Subjects: showing the \(top) costliest of \($n); the JSON on stdout has them all."]
       else [] end)
  + (if $t.partial_runs > 0 then
      ["COST-UNKNOWN lists the runs that emitted no result event, typically cancelled: their tokens are counted everywhere, their cost is not recoverable. A '"'"'+'"'"' marks a cost that is a floor rather than the spend."]
     else [] end)
  + (if $t.skipped_runs > 0 then
      ["\($t.skipped_runs) run(s) uploaded no readable claude-session-logs artifact and are absent entirely: codex-harness runs, runs that ended before the upload, and torn uploads."]
     else [] end)
  + ["Cost at API list prices — a large multiple of the effective rate on Claude Code subscriptions."]
  | .[]
' "$WORKDIR/report.json" >&2
