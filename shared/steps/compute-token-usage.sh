#!/usr/bin/env bash
# Computes the run's token accounting and prints it as one JSON object.
#
# Reads (env):
#   STREAM_JSON - the headless run's stream-json (NDJSON of SDK message events)
#   LOGS_DIR    - consolidated log dir; holds the agent's session JSONL
#   MODEL       - model name, copied through to the output
#
# Output shape mirrors the interactive harness so downstream consumers
# (review-reviewers' evidence gist, token-report.sh, dashboards) don't branch
# on harness.

set -euo pipefail

MODEL=${MODEL:-}
LOGS_DIR=${LOGS_DIR:-}

USAGE=""

# Both parsers below read their NDJSON as raw text and parse line by line
# (`-R -s`, then `map(fromjson? // empty)`) rather than letting jq decode the
# stream. `jq -s` aborts the whole file on the first malformed line, and the
# `|| echo ''` that follows swallows the error — so one bad line would zero the
# run's accounting rather than cost that line. A killed process truncating its
# final append is the likeliest way to get one, which is exactly the
# cancellation case this script exists to account for.

# Primary path: the stream-json's `type: "result"` events. Sessions that use
# `run_in_background: true` Bash emit a second `result` on wakeup; `usage.*`
# and `num_turns` are per-event, while `total_cost_usd` is cumulative. Sum the
# per-event fields across all entries and take cost from the last.
if [ -n "${STREAM_JSON:-}" ] && [ -s "$STREAM_JSON" ]; then
  USAGE=$(jq -R -s -c --arg model "$MODEL" '
    split("\n") | map(fromjson? // empty) |
    (map(select(.type == "result"))) as $rs |
    if ($rs | length) == 0 then
      empty
    else
      ($rs | last) as $r |
      {
        input_tokens: ([$rs[].usage.input_tokens // 0] | add),
        output_tokens: ([$rs[].usage.output_tokens // 0] | add),
        cache_creation_input_tokens: ([$rs[].usage.cache_creation_input_tokens // 0] | add),
        cache_read_input_tokens: ([$rs[].usage.cache_read_input_tokens // 0] | add),
        turns: ([$rs[].num_turns // 0] | add),
        model: $model,
        cost_usd: (($r.total_cost_usd // 0) * 100 | round / 100),
        partial: false
      }
    end
  ' "$STREAM_JSON" 2>/dev/null || echo '')
fi

# Fallback: no `result` event. A cancelled session never emits one, and
# `tend-review` runs with `cancel-in-progress: true`, so this is routine rather
# than exotic — the run may have done dozens of turns and already posted its
# review. Reconstruct from the session JSONL the step has just consolidated
# into LOGS_DIR (uploaded for every repo, unlike the raw stream-json).
#
# Read the session JSONL, NOT the stream-json, even though both carry
# `type: "assistant"` events. The stream-json's are non-final
# (`stop_reason: null`): `usage.output_tokens` is the message-start
# placeholder — single digits against thousands — while the input and cache
# fields, known at message start, do match. Summing the stream's events would
# under-count output by orders of magnitude and look plausible doing it. The
# session JSONL's per-message usage reproduces the `result` event's four token
# fields exactly.
#
# Both files record each assistant message roughly twice, hence unique_by(.id).
#
# Skip `<session-id>/subagents/agent-*.jsonl` — each `Task` subagent gets its
# own transcript there, and `cp -a .../projects/.` brings the subtree along.
# The `result` event this path reconstructs counts only the main loop, so
# slurping the subagents alongside it inflates every field (turns roughly
# doubles) and makes partial runs incomparable with complete ones.
if [ -z "${USAGE:-}" ] && [ -n "$LOGS_DIR" ] && [ -d "$LOGS_DIR" ]; then
  mapfile -t SESSION_FILES < <(find "$LOGS_DIR" -name '*.jsonl' -type f -not -path '*/subagents/*' | sort)
  if [ ${#SESSION_FILES[@]} -gt 0 ]; then
    # `awk 1` rather than jq's own file arguments: `-R -s` concatenates the
    # files into one string before `split("\n")` sees it, so a file that ends
    # without a newline — the truncation case above — would glue its last line
    # to the next file's first line and `fromjson?` would drop both. awk ends
    # every file on a newline, so a truncated tail costs only itself.
    USAGE=$(awk 1 "${SESSION_FILES[@]}" | jq -R -s -c --arg model "$MODEL" \
      --argjson sessions "${#SESSION_FILES[@]}" '
      split("\n") | map(fromjson? // empty) |
      ([.[] | select(.type == "assistant" and .message.id != null)
            | {id: .message.id, u: .message.usage}] | unique_by(.id)) as $ms |
      if ($ms | length) == 0 then
        empty
      else
        {
          input_tokens: ([$ms[].u.input_tokens // 0] | add // 0),
          output_tokens: ([$ms[].u.output_tokens // 0] | add // 0),
          cache_creation_input_tokens: ([$ms[].u.cache_creation_input_tokens // 0] | add // 0),
          cache_read_input_tokens: ([$ms[].u.cache_read_input_tokens // 0] | add // 0),
          # The prompt that opens a session is a `user` line but not a turn, and
          # the files are pooled by the time this runs — so subtract one per
          # session, not one overall.
          turns: ([([.[] | select(.type == "user")] | length) - $sessions, 0] | max),
          model: $model,
          # Only `result.total_cost_usd` carries cost, and that is the event we
          # do not have. `null` says unknown; a `0` here would repeat the bug
          # this fallback exists to fix, one field down.
          cost_usd: null,
          partial: true
        }
      end
    ' 2>/dev/null || echo '')
  fi
fi

# Neither a result event nor any assistant message: the agent never ran (a
# preflight failure, say). That run genuinely cost nothing, so report a real
# zero rather than flagging an unknown.
if [ -z "${USAGE:-}" ]; then
  USAGE=$(jq -n -c --arg model "$MODEL" '
    {input_tokens:0, output_tokens:0, cache_creation_input_tokens:0,
     cache_read_input_tokens:0, turns:0, model:$model, cost_usd:0,
     partial:false}')
fi

printf '%s\n' "$USAGE"
