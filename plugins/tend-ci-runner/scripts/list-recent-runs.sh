#!/usr/bin/env bash
# Lists recently completed tend CI runs.
#
# Fetches runs started since two hours before the completion window's floor,
# then filters to only those that are completed and whose updatedAt falls
# within that window. This two-step approach is needed because
# `gh run list --created` filters by *start* time, not *end* time — a run
# started 2h ago may have just finished, and a run started 50min ago may
# still be running.
#
# Window anchor: when invoked under a scheduled workflow with a fixed-period
# cron — hourly (`MM * * * *`) or an every-N-hours step (`MM */N * * *`, N
# dividing 24) — the completion window is anchored to the most recent intended
# cron tick instead of `now`, and is one cron period wide. Consecutive cycles
# then tile exactly: [intended-period, intended], then [intended,
# intended+period]. Without this, GHA scheduler delay (20-40 min during peak
# hours) shifts each cycle's window relative to actual start time and drops
# runs that finished in the slack between consecutive actual starts. When GHA
# *drops* a tick entirely (not just delays it), the window's floor is instead
# pulled back to the previous actual run's intended tick so the orphaned period
# still gets analyzed. For non-schedule events or cron shapes with no constant
# period, falls back to a now-anchored 1h window. Either way the window's floor
# is printed to stderr as `Completion window: >= <timestamp>`, since callers
# filter on it and can't recover it from the run list.
#
# Environment variables:
#   TARGET_REPO - Query a different repo (default: current repo)
#
# Output: JSON array of {databaseId, conclusion, createdAt, updatedAt} objects.

set -euo pipefail

# Prevent gh from emitting ANSI color codes in non-TTY contexts.
export NO_COLOR=1

# Retry a command up to 3 times on failure, echoing its stdout on success.
# Transient GitHub API errors (e.g. HTTP 503 during an Actions incident) would
# otherwise slip past `set -euo pipefail` at the call sites below — a process
# substitution's failure is invisible to `set -e`, and a `|| echo "[]"` fallback
# silently turns an errored fetch into an empty result. Either way the script
# would report zero runs when runs exist, and the calling skill records a false
# "all-clear" that permanently skips that window. Retry here, and let the caller
# fail loud if every attempt fails rather than swallow the error.
gh_retry() {
  local out attempt
  for attempt in 1 2 3; do
    if out=$("$@" 2>/dev/null); then
      printf '%s' "$out"
      return 0
    fi
    # Don't sleep after the final attempt — the whole point of this script is
    # to fail loud and fast during an incident, so a trailing backoff before
    # the caller's `exit 1` is wasted delay.
    [ "$attempt" -lt 3 ] && sleep $((attempt * 3))
  done
  return 1
}

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

if ! wf_json=$(gh_retry gh workflow list "${repo_args[@]}" --json name); then
  echo "ERROR: 'gh workflow list' failed after retries (transient API error?) — refusing to report an empty run list that would read as a false all-clear" >&2
  exit 1
fi

WORKFLOWS=()
for prefix in "${PREFIXES[@]}"; do
  mapfile -t matches < <(printf '%s' "$wf_json" | jq -r ".[].name | select(startswith(\"$prefix\"))")
  WORKFLOWS+=("${matches[@]}")
done

# Detect a fixed-period cron from the workflow event payload so we can anchor
# the window to the most recent intended tick: either hourly ("47 * * * *") or
# an every-N-hours step ("47 */3 * * *").
#
# The step form is only accepted when N divides 24. Cron's `*/N` restarts the
# count at hour 0 each day, so a step that doesn't divide evenly (e.g. `*/5`
# fires at 0,5,10,15,20 then wraps after 4h) has no constant period, and a
# floor computed from one would silently under-reach across midnight. Those
# fall through to the now-anchored window below, same as any other cron shape.
cron_minute=""
cron_hour_step=1
if [ -f "${GITHUB_EVENT_PATH:-}" ]; then
  schedule=$(jq -r '.schedule // empty' "$GITHUB_EVENT_PATH" 2>/dev/null || true)
  if [[ "$schedule" =~ ^([0-9]+)\ \*\ \*\ \*\ \*$ ]]; then
    cron_minute="${BASH_REMATCH[1]}"
  elif [[ "$schedule" =~ ^([0-9]+)\ \*/([0-9]+)\ \*\ \*\ \*$ ]] \
    && [ "${BASH_REMATCH[2]}" -gt 0 ] && [ $((24 % BASH_REMATCH[2])) -eq 0 ]; then
    cron_minute="${BASH_REMATCH[1]}"
    cron_hour_step="${BASH_REMATCH[2]}"
  fi
fi

if [ -n "$cron_minute" ]; then
  # One cron period in seconds. `cron_hour_step` is 1 for the hourly form, so
  # every expression below reduces to the original hourly arithmetic.
  period=$((cron_hour_step * 3600))
  # Hour-of-day of the most recent tick: the largest multiple of the step at or
  # before the current hour. For the hourly form this is just the current hour.
  this_tick_hour=$(( ($(date -u +%-H) / cron_hour_step) * cron_hour_step ))
  this_hour_tick=$(date -u -d "$(date -u +%Y-%m-%d)T$(printf '%02d' "$this_tick_hour"):00:00 $cron_minute minutes" +%s)
  now_ts=$(date -u +%s)
  if [ "$now_ts" -lt "$this_hour_tick" ]; then
    intended=$((this_hour_tick - period))
  else
    intended=$this_hour_tick
  fi
  # Default floor: one cron period back. Consecutive ticks tile exactly.
  COMPLETED_AFTER=$((intended - period))

  # Dropped-tick recovery. GHA doesn't only *delay* scheduled ticks, it also
  # *drops* them: a tick that fires zero times leaves that hour's completions
  # in the gap between the previous and next cycle's windows (the skipped-tick
  # case #526 deferred as acceptable). Rather than assume the previous tick
  # fired, resume from where the previous *actual* completed run of this
  # workflow left off: recover that run's intended tick and floor the window
  # there. When every tick fires, the previous run's intended tick == the
  # default (intended - period), so this is a byte-identical no-op — still no
  # overlap between consecutive cycles. When a tick was dropped, it reaches
  # back to cover the orphaned period. Capped at 6h so a sustained outage can't
  # create an unbounded window — six full periods at hourly, and still two at
  # the 3-hourly step, so a single dropped tick stays recoverable either way.
  # The analyzing workflow runs on the current repo, so this query omits
  # TARGET_REPO's -R.
  if [ -n "${GITHUB_WORKFLOW:-}" ]; then
    prev_start=$(gh run list --workflow "$GITHUB_WORKFLOW" --status completed \
      --limit 10 --json databaseId,createdAt \
      --jq "[.[] | select(.databaseId != (${GITHUB_RUN_ID:-0}))] | .[0].createdAt // empty" \
      2>/dev/null || true)
    if [ -n "$prev_start" ]; then
      prev_ts=$(date -u -d "$prev_start" +%s 2>/dev/null || echo "")
      if [ -n "$prev_ts" ]; then
        prev_tick_hour=$(( ($(date -u -d "@$prev_ts" +%-H) / cron_hour_step) * cron_hour_step ))
        prev_hour_tick=$(date -u -d "$(date -u -d "@$prev_ts" +%Y-%m-%d)T$(printf '%02d' "$prev_tick_hour"):00:00 $cron_minute minutes" +%s)
        if [ "$prev_ts" -ge "$prev_hour_tick" ]; then
          prev_intended=$prev_hour_tick
        else
          prev_intended=$((prev_hour_tick - period))
        fi
        floor_cap=$((intended - 21600))   # never reach back more than 6h
        [ "$prev_intended" -lt "$floor_cap" ] && prev_intended=$floor_cap
        [ "$prev_intended" -lt "$COMPLETED_AFTER" ] && COMPLETED_AFTER=$prev_intended
      fi
    fi
  fi

  CREATED_SINCE=$(date -u -d "@$((COMPLETED_AFTER - 7200))" +%Y-%m-%dT%H:%M:%S)
else
  CREATED_SINCE=$(date -d '3 hours ago' +%Y-%m-%dT%H:%M:%S)
  COMPLETED_AFTER=$(date -d '1 hour ago' +%s)
fi

# Publish the window. Callers need the floor for their own time filtering, and
# without it a fallback-branch run (any non-schedule event, e.g. a manual
# workflow_dispatch) looks the same as a scheduled one while covering only the
# last hour of a possibly longer period. stderr keeps stdout the run-list JSON.
echo "Completion window: >= $(date -u -d "@$COMPLETED_AFTER" +%Y-%m-%dT%H:%M:%SZ)" >&2

all_runs="[]"

for wf in "${WORKFLOWS[@]}"; do
  if ! runs=$(gh_retry gh run list \
    "${repo_args[@]}" \
    --workflow "${wf}" \
    --created ">=${CREATED_SINCE}" \
    --json databaseId,conclusion,createdAt,updatedAt \
    --limit 50); then
    echo "ERROR: 'gh run list' for workflow '$wf' failed after retries — refusing to report a partial run list" >&2
    exit 1
  fi
  all_runs=$(echo "$all_runs" "$runs" | jq -s 'add')
done

# Filter: drop in-progress (empty conclusion), keep only recently finished
echo "$all_runs" | jq --argjson cutoff "$COMPLETED_AFTER" '
  [ .[]
    | select(.conclusion != null and .conclusion != "")
    | select((.updatedAt | fromdateiso8601) >= $cutoff)
  ]
'
