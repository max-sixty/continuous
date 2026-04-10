---
name: review-reviewers
description: Hourly analysis of Claude CI session logs — identifies behavioral problems, skill gaps, and workflow issues.
argument-hint: "<owner/repo>"
metadata:
  internal: true
---

# Review Reviewers

Analyze Claude-powered CI runs from the past hour. Identify behavioral problems, skill gaps, and
workflow issues — then create PRs or issues to fix them.

## Core principle: repo-specific guidance is primary

Each adopter repo has its own guidance (`running-tend` skill or equivalent) that shapes how the bot
should behave in that repo. This repo-specific guidance **takes precedence** over tend's default
rules. The bot's job is to follow the repo-specific guidance first, falling back to tend's defaults
only where the repo doesn't specify.

When reviewing a session, always load and read the target repo's repo-specific guidance before
evaluating whether the bot behaved correctly. An action that would violate tend's defaults (e.g.,
closing an issue) is correct if the repo's guidance explicitly authorized it. Conversely, an action
that follows tend's defaults but contradicts repo-specific guidance is a problem.

Frame your analysis around this hierarchy: did the bot follow the repo's guidance? Only fall back
to evaluating against tend's defaults for behaviors the repo doesn't address.

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

## Target repo

**Target repo:** $ARGUMENTS

Analysis targets an adopter repo whose CI runs are analyzed. Findings result in PRs/issues on the
current repo (tend) to improve skills and workflows.

Use `-R $ARGUMENTS` for commands that access the target repo (downloading artifacts, querying runs
and PRs). Commands without `-R` default to tend.

@review-gates.md

Use `TRACKING_LABEL="review-reviewers-tracking"` for this skill's tracking issues. Use
`-R $ARGUMENTS` when downloading session logs for historical evidence verification.

## Step 1: Find recent runs

List recently completed Claude CI runs on the target repo:

```bash
TARGET_REPO=$ARGUMENTS ${CLAUDE_PLUGIN_ROOT}/scripts/list-recent-runs.sh
```

The script discovers `tend-*` workflows by default. Pass additional prefixes as arguments to
include other workflows (e.g., `review-reviewers` when analyzing tend itself).

If empty, report "no runs to review" and exit.

## Step 2: Load repo-specific guidance and download session logs

First, read the target repo's repo-specific guidance to understand what the bot was told to do.
`gh api` does not accept `-R` — embed the repo in the endpoint path instead:

```bash
gh api "repos/$ARGUMENTS/contents/.claude/skills/running-tend/SKILL.md" \
  --jq '.content' | base64 -d
```

If the file doesn't exist, try common alternatives (`.claude/skills/running-tend.md`,
`.claude/CLAUDE.md`). Understanding the repo's guidance is essential context for evaluating every
session — without it, you'll misjudge authorized behavior as a violation.

Then load `/install-tend:debug-ci-session` for download commands and JSONL parsing queries. Use
`-R $ARGUMENTS` for `gh run`, `gh pr`, and `gh issue` commands targeting the adopter repo. For
`gh api` calls, substitute the repo into the endpoint path (as shown above) since the `-R` flag is
not supported there.

Skip runs without artifacts. Trace decision chains: what did Claude decide, what evidence did it
use, what was the outcome?

## Step 3: Cross-check review sessions

For `tend-review` runs, compare what the bot said against what happened next:

```bash
HEAD_BRANCH=$(gh -R $ARGUMENTS run view <run-id> --json headBranch --jq '.headBranch')
PR_NUMBER=$(gh -R $ARGUMENTS pr list --head "$HEAD_BRANCH" --state all --json number --jq '.[0].number')
```

Check for subsequent commits that undid something the bot approved (gap in review), and human
review comments flagging issues the bot missed. Pull in the full PR context — not just changes
from the past hour.

CI polling time is expected and acceptable — do not flag it.

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
  `claude-behavior`. Put full analysis in PR description (run ID, log excerpts, root cause, **gate
  assessment** including historical evidence count). Don't also create a separate issue.
- **Issue** (fallback): Only for problems too large or ambiguous to fix directly. Include run ID,
  log excerpts, root cause analysis.

Group multiple findings by broad theme. **Limit to at most 2 PRs per run** — if you have more
findings, pick the highest-confidence ones and note the rest in the tracking issue.

## Step 6: Summary

If no problems found (or none passed the gates), report "all clear" with: runs analyzed, sessions
reviewed, brief quality assessment, and any below-threshold findings recorded in the tracking
issue.
