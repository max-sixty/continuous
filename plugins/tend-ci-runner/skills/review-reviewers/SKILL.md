---
name: review-reviewers
description: Hourly outcome-based analysis of Claude CI behavior — checks whether bot outputs were accepted or rejected, escalating to session logs only when outcomes look wrong.
argument-hint: "<owner/repo>"
metadata:
  internal: true
---

# Review Reviewers

Analyze Claude-powered CI behavior on the target repo over the past hour. Focus on **outcomes** —
what the bot produced publicly and whether it was accepted — rather than internal session mechanics.
Create PRs or issues on tend when outcomes reveal behavioral problems.

## Cost discipline: Haiku subagents for exploration

Session log parsing and outcome checking are token-heavy. Delegate all broad exploration to **Haiku
subagents** (`Agent` tool with `model: "haiku"`). Keep the main Opus agent for judgment: evaluating
findings against gates, deciding whether to act, and drafting PRs.

Pattern:
1. Opus sets up context (bot identity, repo guidance, run list)
2. Opus spawns Haiku subagent to survey outcomes across all runs → receives structured summary
3. Opus evaluates the summary against gates
4. If needed, Opus spawns another Haiku subagent to investigate specific session logs → receives
   diagnosis
5. Opus drafts fix PR if warranted

## Core principle: outcomes over internals

The bot's job is to produce useful outputs: reviews, triage comments, fix commits, issue responses.
The cheapest way to evaluate quality is to check whether those outputs were **accepted** (merged,
kept, acted on) or **rejected** (reverted, closed, corrected, disagreed with).

Session logs are expensive to download and parse. Only escalate to session-log inspection when
outcome signals indicate a real problem worth diagnosing.

## Core principle: repo-specific guidance is primary

Each adopter repo has its own guidance (`running-tend` skill or equivalent) that shapes how the bot
should behave in that repo. This repo-specific guidance **takes precedence** over tend's default
rules. The bot's job is to follow the repo-specific guidance first, falling back to tend's defaults
only where the repo doesn't specify.

## Non-issues: do not flag these

Some patterns look suspicious but are intentional. Before drafting a finding, check this list —
flagging expected behaviors creates maintainer churn and costs trust.

- **`tend-review` re-approving after the bot pushed a fix commit.** The reviewer role is
  independent of commit and PR authorship. Re-reviewing (and re-approving) after
  `tend-notifications`, `tend-ci-fix`, or a mention run pushes a fix is expected behavior, not a
  re-approval loop. Two prior PRs attempted authorship-keyed guards and were both closed by the
  maintainer as solving a non-problem — [#154](https://github.com/max-sixty/tend/pull/154)
  ("skip re-review when bot pushes to already-approved PR") and
  [#212](https://github.com/max-sixty/tend/pull/212) ("skip APPROVE when incremental commits are
  bot-authored"). If you observe stacked approvals from concurrent runs that raced with
  concurrency-group cancellation, that is a *concurrency* issue (the cancelled runs managed to
  POST before the SIGTERM arrived) — do not propose changes to review's approval rules.

- **`tend-mention` firing on the bot's own comments and exiting silently.** When the bot comments
  on an issue or PR where it has previously participated (including its own tracking issues such
  as `review-reviewers-tracking` and `review-runs-tracking`), the `issue_comment` event fires
  `tend-mention`; the prompt's self-conversation guard then detects the self-trigger and exits
  silently after a few Claude turns. This looks wasteful (each exit costs ~$0.20–$0.50), but
  [#203](https://github.com/max-sixty/tend/pull/203) ("fix: filter bot self-triggers in
  tend-mention workflow") added sender-based filters for `pull_request_review` /
  `pull_request_review_comment` events and was closed by the maintainer without merge — the same
  authorship-keyed-guard pattern rejected in #154 and #212. Do not propose sender-based or
  commenter-based filters to `tend-mention`. The outage-loop guard added in
  [#268](https://github.com/max-sixty/tend/pull/268) (skipping `tend-outage`-labeled issues) is
  the accepted shape for a label-based skip — propose new filters only when there's a distinct
  loop risk that can't be expressed with a label.

## Target repo

**Target repo:** $ARGUMENTS

Analysis targets an adopter repo whose CI runs are analyzed. Findings result in PRs/issues on the
current repo (tend) to improve skills and workflows.

Use `-R $ARGUMENTS` for commands that access the target repo (querying runs, PRs, issues). Commands
without `-R` default to tend.

@review-gates.md

Use `TRACKING_LABEL="review-reviewers-tracking"` for this skill's tracking issues.

## Step 1: Setup

Resolve the **target repo's** bot login and load repo-specific guidance upfront — both are
needed throughout. `gh api user` returns the *analysis* bot (e.g., `tend-agent` when
review-reviewers runs on tend), which is typically **not** the target repo's bot
(e.g., `worktrunk-bot`) — filtering reviews/comments by the wrong login produces false
"no bot output" negatives. Read `bot_name` from the target repo's `.config/tend.toml`:

```bash
BOT_LOGIN=$(gh api "repos/$ARGUMENTS/contents/.config/tend.toml" --jq '.content' 2>/dev/null \
  | base64 -d 2>/dev/null \
  | grep -E '^bot_name\s*=' | head -1 | sed -E 's/.*=\s*"?([^"]+)"?.*/\1/')
if [ -z "$BOT_LOGIN" ]; then
  echo "ERROR: could not resolve bot_name from $ARGUMENTS/.config/tend.toml" >&2
  exit 1
fi
echo "BOT_LOGIN=$BOT_LOGIN (target: $ARGUMENTS)"
```

Read the target repo's repo-specific guidance to understand what the bot was told to do:

```bash
gh api "repos/$ARGUMENTS/contents/.claude/skills/running-tend/SKILL.md" \
  --jq '.content' | base64 -d
```

If the file doesn't exist, try common alternatives (`.claude/skills/running-tend.md`,
`.claude/CLAUDE.md`). Understanding the repo's guidance is essential context for evaluating outcomes
— without it, you'll misjudge authorized behavior as a violation.

Then list recently completed Claude CI runs on the target repo:

```bash
TARGET_REPO=$ARGUMENTS ${CLAUDE_PLUGIN_ROOT}/scripts/list-recent-runs.sh
```

The script discovers `tend-*` workflows by default. Pass additional prefixes as arguments to
include other workflows (e.g., `review-reviewers` when analyzing tend itself).

If empty, report "no runs to review" and exit.

## Step 2: Survey outcomes via Haiku subagent

Spawn a Haiku subagent to check outcomes across all runs from Step 1. The subagent does the
token-heavy work of mapping runs to PRs/issues and checking acceptance signals.

Use `Agent` with `model: "haiku"` and a prompt like:

> Survey bot outcomes on `$ARGUMENTS` for the following runs: [run IDs from Step 1].
> The bot's login is `$BOT_LOGIN`.
>
> For each run, determine:
> 1. Did the bot produce visible output (review, comment, issue action, commit)?
> 2. If yes, was the output accepted or rejected?
>
> **How to map runs to outputs:**
> - `tend-review`: `gh -R $ARGUMENTS run view <run-id> --json headBranch` → find PR via
>   `gh -R $ARGUMENTS pr list --head <branch> --state all` → check bot reviews via
>   `gh api repos/$ARGUMENTS/pulls/<pr>/reviews`
> - `tend-notifications`: check for recent bot comments/issue-close events in the past hour
> - `tend-mention`: map run to issue/PR from triggering comment, check for bot replies
> - `tend-ci-fix`: map run → PR via `headBranch`, check for bot commits
>
> **Negative outcome signals** (report these):
> - Human reviewer posted CHANGES_REQUESTED after bot approved
> - PR closed without merge shortly after bot approved
> - Bot posted no review despite a `tend-review` run completing on an open PR
> - Subsequent commits reversed changes the bot approved
> - Bot-closed issue was reopened
> - Fix commit was reverted or CI still failing after bot pushed
> - Human replied to bot with correction or complaint
> - Bot comment contains corruption (literal `${`, unescaped bangs, broken heredoc markers)
>
> **Report format** — return a structured summary:
> ```
> ## Runs with no bot output (skipped)
> - <run-id>: <workflow> — <reason> (e.g., "no artifacts", "notification no-op")
>
> ## Runs with accepted output
> - <run-id>: <workflow> on PR #N — bot reviewed, PR merged
>
> ## Runs with concerning output
> - <run-id>: <workflow> on PR #N — <signal> (e.g., "human posted CHANGES_REQUESTED")
>
> ## Sanity check
> <note if zero bot activity found across all runs — may indicate systemic failure>
> ```

Review the subagent's summary. If all outputs are accepted and no sanity-check flags, skip to
Step 6 (summary). If concerning outcomes exist, continue to Step 3.

## Step 3: Investigate concerning outcomes via Haiku subagent

For runs with negative outcome signals (or suspicious lack of output), spawn another Haiku subagent
to download and inspect the specific session logs.

Use `Agent` with `model: "haiku"` and a prompt like:

> Investigate session logs for run <run-id> on `$ARGUMENTS`.
>
> Download: `gh run download <run-id> -R $ARGUMENTS --pattern 'claude-session-logs*' --dir /tmp/session-logs/<run-id>/`
>
> The concerning outcome was: <signal from Step 2>.
>
> **JSONL parsing** — each line has a `type` field (`user`, `assistant`, `system`). Key queries:
> ```
> # Tool calls in order
> jq -r 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use") | "\(.name): \(.input | tostring | .[0:120])"' FILE
> # Assistant reasoning
> jq -r 'select(.type == "assistant") | .message.content[]? | select(.type == "text") | .text' FILE
> # Bash commands executed
> jq -r 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use" and .name == "Bash") | .input.command' FILE
> ```
>
> Focus narrowly: what decision did the bot make that led to this bad outcome? Trace the decision
> chain in the JSONL for the specific problematic action. Don't parse the entire session.
> CI polling (sleep loops checking `gh pr checks`) in session logs is expected bot behavior — do
> not flag it.
>
> Report: what the bot decided, what evidence it used, and what went wrong.

Evaluate the subagent's diagnosis against the repo-specific guidance from Step 1. Determine whether
the failure is structural (same conditions always produce this failure) or stochastic (probabilistic
model behavior that might not recur).

## Step 4: Deduplicate

Before creating issues or PRs, check exhaustively for existing ones:

```bash
gh issue list --state open --label claude-behavior --json number,title,body
gh issue list --state open --json number,title,body  # also check unlabeled issues
gh pr list --state open --json number,title,headRefName,body
gh issue list --state closed --label claude-behavior --json number,title,closedAt --limit 30
```

Search titles AND bodies for related keywords. Only comment on existing issues if you have
material new cases that would change the approach or increase prioritization. Do not comment with
progress updates, fix-PR status, or re-statements of evidence already in the issue.

## Step 5: Act on findings

**Prefer PRs over issues.** A PR with a clear description is immediately actionable.

- **PR** (default): Branch `hourly/review-$GITHUB_RUN_ID`, fix, commit, push, create with label
  `claude-behavior`. Put full analysis in PR description (run ID, outcome evidence, root cause,
  **gate assessment** including historical evidence count). Don't also create a separate issue.
- **Issue** (fallback): Only for problems too large or ambiguous to fix directly. Include run ID,
  outcome evidence, root cause analysis.

Group multiple findings by broad theme. **Limit to at most 2 PRs per run** — if you have more
findings, pick the highest-confidence ones and note the rest in the tracking issue.

**Do not poll CI** after creating a PR. The `tend-review` and `tend-ci-fix` workflows handle PRs
independently. Exit after pushing and creating the PR.

## Step 6: Summary

Report results in the conversation log and save a markdown summary to `/tmp/claude/step-summary.md`
(a post-Claude step copies this into the GitHub Actions step summary):

```bash
mkdir -p /tmp/claude
cat > /tmp/claude/step-summary.md << 'EOF'
## Review-reviewers summary
...
EOF
```

If no problems found (or none passed the gates), report "all clear" with: runs analyzed, outcomes
checked, brief quality assessment, and any below-threshold findings recorded in the tracking issue.
