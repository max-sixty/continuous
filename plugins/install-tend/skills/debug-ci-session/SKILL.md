---
name: debug-ci-session
description: Debugs Claude CI runs by downloading and parsing session log artifacts from GitHub Actions. Use when asked to "debug a CI run", "check what the bot did", "look at session logs", "investigate a tend run", "why did the bot do X", "what happened in CI", or to trace bot behavior in a specific workflow run.
---

# Debug CI Session

Investigate what a Claude-powered CI bot did during a GitHub Actions run by
downloading and parsing its session log artifacts.

## Identify the run

If the user provides a run ID or URL, extract the numeric run ID. Otherwise,
list recent tend runs:

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
gh run list -R "$REPO" --limit 20 \
  --json databaseId,name,conclusion,createdAt,headBranch,event \
  --jq '.[] | select(.name | startswith("tend-")) |
    "\(.databaseId)\t\(.conclusion)\t\(.createdAt)\t\(.name)\t\(.headBranch)\t\(.event)"'
```

Narrow by branch (`--branch`), event type, or workflow name as needed. To find
the run associated with a specific PR:

```bash
PR_NUMBER=<number>
HEAD=$(gh pr view "$PR_NUMBER" -R "$REPO" --json headRefName --jq '.headRefName')
gh run list -R "$REPO" --branch "$HEAD" --limit 10 \
  --json databaseId,name,conclusion,createdAt,event \
  --jq '.[] | select(.name | startswith("tend-")) |
    "\(.databaseId)\t\(.conclusion)\t\(.name)\t\(.event)"'
```

## Download session logs

Session logs are uploaded as artifacts named `claude-session-logs*`. Each
artifact contains one or more JSONL files — one per Claude session in that run.

```bash
RUN_ID=<run-id>
gh run download "$RUN_ID" -R "$REPO" --pattern 'claude-session-logs*' --dir /tmp/session-logs/"$RUN_ID"/
```

If no artifacts exist, the run either had no Claude session or the session was
too short to produce logs. Check the run's console output as a fallback:

```bash
gh run view "$RUN_ID" -R "$REPO" --log-failed
```

## Parse session logs

Each JSONL line has a `type` field: `user`, `assistant`, or `system`.

### Overview — what happened

Start with a high-level trace of the session:

```bash
FILE=/tmp/session-logs/$RUN_ID/<session>.jsonl

# Skills loaded
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "tool_use" and .name == "Skill") |
  .input.skill' "$FILE"

# Tool calls in order
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "tool_use") |
  "\(.name): \(.input | tostring | .[0:120])"' "$FILE"

# Assistant text (reasoning and responses)
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "text") | .text' "$FILE"
```

### Targeted queries

```bash
# What the bot was told (user messages including injected prompts)
jq -r 'select(.type == "user") |
  .message.content | if type == "string" then . else
  [.[]? | select(.type == "text") | .text] | join("\n") end' "$FILE"

# Bash commands executed
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "tool_use" and .name == "Bash") |
  .input.command' "$FILE"

# Tool results (what the bot saw back)
jq -r 'select(.type == "user") | .message.content[]? |
  select(.type == "tool_result") |
  "\(.tool_use_id): \(.content | tostring | .[0:200])"' "$FILE"

# Files read
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "tool_use" and .name == "Read") |
  .input.file_path' "$FILE"

# Files written or edited
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "tool_use" and (.name == "Write" or .name == "Edit")) |
  "\(.name): \(.input.file_path)"' "$FILE"

# GitHub API calls (gh commands)
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "tool_use" and .name == "Bash") |
  .input.command | select(startswith("gh ") or contains("| gh "))' "$FILE"
```

### Searching for specific behavior

```bash
# Find where the bot mentioned a keyword
jq -r 'select(.type == "assistant") | .message.content[]? |
  select(.type == "text") | .text | select(test("KEYWORD"; "i"))' "$FILE"

# Find tool calls with specific input
jq -c 'select(.type == "assistant") | .message.content[]? |
  select(.type == "tool_use") |
  select(.input | tostring | test("KEYWORD"; "i"))' "$FILE"
```

## Diagnose the problem

After extracting the session trace, reconstruct the decision chain:

1. **What triggered the run?** Check the event type and triggering context
   (PR comment, push, schedule).
2. **What did the bot see?** Look at system/user messages and tool results.
3. **What did it decide?** Follow assistant text for reasoning.
4. **Where did it go wrong?** Compare intended behavior against actual tool
   calls and outputs.

Common failure modes:
- **Wrong skill loaded** (or skill not loaded) — check the Skill tool calls
- **Stale context** — bot acted on outdated PR state or missed recent commits
- **Tool error ignored** — a Bash command failed but the bot continued
- **Hallucinated file/function** — bot referenced something that doesn't exist
- **CI polling timeout** — bot ran out of time waiting for checks

## Cross-reference with PR state

For review runs, compare the bot's actions against the PR timeline:

```bash
PR_NUMBER=<number>
gh pr view "$PR_NUMBER" -R "$REPO" --json title,state,reviews,comments,commits \
  --jq '{title, state, reviews: [.reviews[] | {author: .author.login, state: .state}],
    comments: (.comments | length), commits: (.commits | length)}'
```

Check whether subsequent commits undid something the bot approved, or whether
human reviewers flagged issues the bot missed.
