# Parsing Codex session logs

Use these recipes when the artifact is `codex-session-logs-*`. `$FILE` is
the rollout JSONL path set in the skill's download step.

Each JSONL line has a top-level `type` of `session_meta`, `turn_context`,
`event_msg`, or `response_item`. Response content sits under
`response_item.payload`, with the variant in `.payload.type`:

- `message` — initial input from `user` or `developer` (system prompt,
  AGENTS.md, skill listings); each content item is internally tagged
  `{type: "input_text", text}`, so text is at
  `.payload.content[] | select(.type == "input_text") | .text`
- `agent_message` — text emitted by the model during the turn
  (`.payload.message`)
- `custom_tool_call` — tool invocation; `.payload.name` plus the tool's
  string input at `.payload.input`
- `custom_tool_call_output` — paired result; `.payload.call_id` plus text
  items at `.payload.output[] | select(.type == "input_text") | .text`
- `reasoning` — opaque encrypted blob; skip

Lifecycle metadata such as `task_started`, `task_complete`, and `token_count`
sits under `event_msg.payload`. `task_complete.last_agent_message` carries the
final reply.

The bot drives shell through the `exec` tool. Its `.payload.input` is
JavaScript that calls nested tools such as `tools.exec_command({...})` and
`tools.apply_patch(...)`; inspect the string directly rather than parsing it
with `fromjson`. Long-running commands can also call `tools.write_stdin`.
Codex has no dedicated Read/Write/Edit tool; file I/O appears inside these
tool inputs.

## Overview — what happened

```bash
# Skills loaded (Codex reads SKILL.md via shell rather than a dedicated tool)
jq -r 'select(.payload.type == "custom_tool_call" and .payload.name == "exec") |
  .payload.input | select(test("SKILL\\.md"))' "$FILE"

# Final summary the bot returned
jq -r 'select(.payload.type == "task_complete") | .payload.last_agent_message' "$FILE"

# Tool calls in order
jq -r 'select(.payload.type == "custom_tool_call") |
  "\(.payload.name): \(.payload.input | .[0:160])"' "$FILE"

# Interim model narrative (its visible reasoning; the encrypted blob is opaque)
jq -r 'select(.payload.type == "agent_message") | .payload.message' "$FILE"
```

## Targeted queries

```bash
# All shell commands
jq -r 'select(.payload.type == "custom_tool_call" and .payload.name == "exec") |
  .payload.input' "$FILE"

# Pair each command with its (truncated) output. Parens are required:
# jq binds `,` tighter than `|`, so `A | B, C | D` is `A | (B, C) | D`.
jq -r '(select(.payload.type == "custom_tool_call" and .payload.name == "exec")
        | "→ " + .payload.input),
       (select(.payload.type == "custom_tool_call_output")
        | "← " + ([.payload.output[]? | select(.type == "input_text") | .text]
                    | join("") | .[0:300]))' "$FILE"

# gh CLI calls (including in variable assignments)
jq -r 'select(.payload.type == "custom_tool_call" and .payload.name == "exec") |
  .payload.input | select(test("\\bgh\\b"))' "$FILE"

# File writes / edits (apply_patch, tee, sed -i, redirect to absolute path)
jq -r 'select(.payload.type == "custom_tool_call" and .payload.name == "exec") |
  .payload.input |
  select(test("apply_patch|\\btee\\b|sed -i\\b|>\\s+/"))' "$FILE"

# Initial prompts (AGENTS.md, skill list, triggering event description)
jq -r 'select(.payload.type == "message" and (.payload.role == "user" or .payload.role == "developer")) |
  .payload.content[]? | select(.type == "input_text") | .text' "$FILE"
```

## Searching for specific behavior

```bash
# Model text mentioning a keyword
jq -r 'select(.payload.type == "agent_message") | .payload.message |
  select(test("KEYWORD"; "i"))' "$FILE"

# Commands mentioning a keyword
jq -r 'select(.payload.type == "custom_tool_call" and .payload.name == "exec") |
  .payload.input | select(test("KEYWORD"; "i"))' "$FILE"
```
