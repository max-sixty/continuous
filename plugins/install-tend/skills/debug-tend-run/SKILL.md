---
name: debug-tend-run
description: Investigates a specific tend GitHub Actions run by downloading its session-log artifacts and parsing the JSONL traces. Surfaces which skill tend loaded, what tools it called with what inputs, files it read or wrote, and where decisions went wrong. Use when asked to "debug a tend run", "investigate a tend run", "why did tend do X", "what did the bot do in CI", "look at the session logs", or to reconstruct tend's behavior step-by-step from a run ID, URL, or PR number.
---

# Debug Tend Run

Investigate what tend did during a GitHub Actions run by downloading and
parsing its session log artifacts. Works for both supported harnesses
(Claude and Codex); the artifact name distinguishes them.

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

The artifact name identifies the harness:

- `claude-session-logs*` — Claude harness (headless `claude -p` behind the proxy)
- `codex-session-logs-*` — Codex harness (`max-sixty/tend/codex`)

```bash
RUN_ID=<run-id>
DEST=/tmp/session-logs/$RUN_ID
gh run download "$RUN_ID" -R "$REPO" --pattern '*session-logs*' --dir "$DEST"
ls "$DEST"  # confirm claude- or codex-
FILE=$(find "$DEST" -name '*.jsonl' | head -1)
echo "$FILE"
```

Claude artifacts hold flat JSONL files, one per agent session. Codex
artifacts store the rollout at `sessions/YYYY/MM/DD/rollout-*.jsonl`
plus `projects/token-usage.json`; one rollout per session.

If no artifacts exist, the run either had no agent session or ended before
logs were uploaded. Fall back to console output:

```bash
gh run view "$RUN_ID" -R "$REPO" --log-failed
```

## Parse session logs

The JSONL schema differs by harness. Open the reference matching the
artifact name and follow its recipes:

- `claude-session-logs*` → [`references/claude-logs.md`](references/claude-logs.md)
- `codex-session-logs-*` → [`references/codex-logs.md`](references/codex-logs.md)

Each reference covers the line schema plus copy-paste jq for an overview
trace, targeted queries (commands, tool results, files, gh calls), and
keyword search. Both assume `$FILE` from the download step.

## Diagnose the problem

After extracting the session trace, reconstruct the decision chain:

1. **What triggered the run?** Check the event type and triggering context
   (PR comment, push, schedule).
2. **What did the bot see?** Look at system/user messages and tool results.
3. **What did it decide?** Follow assistant/agent text for reasoning.
4. **Where did it go wrong?** Compare intended behavior against actual tool
   calls and outputs.

Common failure modes:
- **Wrong skill loaded** (or skill not loaded) — inspect skill invocations or
  reads of a `SKILL.md` path in the matching harness log
- **Stale context** — bot acted on outdated PR state or missed recent commits
- **Tool error ignored** — a command failed but the bot continued
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
