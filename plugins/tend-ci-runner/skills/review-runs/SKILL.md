---
name: review-runs
description: Daily review of the previous night's CI runs — identifies problems and improves repo-local skills and workflows.
metadata:
  internal: true
---

# Review Runs

Analyze the previous night's Claude CI runs in this repository. Identify behavioral problems, skill
gaps, and workflow issues — then propose improvements to the repo's local skills and workflows.

This skill runs **in the adopter repo**, not in tend. Improvements target `.claude/skills/` and
`.config/tend.toml` in this repository.

## First steps

```bash
ls .claude/skills/
```

Load any repo-specific skill overlay before proceeding.

@review-gates.md

Use `TRACKING_LABEL="review-runs-tracking"` for this skill's tracking issues.

## Step 1: Find recent runs

List Claude CI runs that completed overnight (past 12 hours):

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
SINCE=$(date -u -d '12 hours ago' +%Y-%m-%dT%H:%M:%SZ)
for workflow in $(gh api repos/$REPO/actions/workflows --jq '.workflows[] | select(.name | startswith("tend-")) | .id'); do
  gh api "repos/$REPO/actions/workflows/$workflow/runs?created=>=$SINCE&status=completed" \
    --jq '.workflow_runs[] | {databaseId: .id, conclusion, createdAt: .created_at, name: .name}'
done
```

If no runs found, report "no runs to review" and exit.

## Step 2: Download and analyze session logs

Load `/install-tend:debug-ci-session` for download commands and JSONL parsing queries.

Skip runs without artifacts. Trace decision chains: what did Claude decide, what evidence did it
use, what was the outcome?

## Step 3: Cross-check outcomes

For each analyzed run, compare what the bot did against what happened next:

- **Review runs**: Did subsequent commits undo something the bot approved? Did human reviewers flag
  issues the bot missed?
- **Triage runs**: Was the bot's classification correct? Did the issue get relabeled?
- **Nightly runs**: Did the bot's PRs get merged, or were they closed as unhelpful?
- **CI-fix runs**: Did the fix actually resolve the CI failure?

```bash
# Example: check if a bot PR was merged or closed
gh pr list --author "$BOT_LOGIN" --state all --json number,title,state,closedAt \
  --jq '.[] | select(.closedAt > "'$SINCE'")'
```

## Step 4: Deduplicate

Before creating issues or PRs, check for existing ones:

```bash
gh issue list --state open --json number,title,body
gh pr list --state open --json number,title,headRefName,body
gh issue list --state closed --json number,title,closedAt --limit 30
```

Search titles AND bodies for related keywords.

## Step 5: Act on findings

Improvements target **repo-local** files:

- **`.claude/skills/`** — update or create skill overlays with guidance that prevents the
  identified problem. Prefer updating existing skill files over creating new ones.
- **`.config/tend.toml`** — adjust workflow configuration if the problem is structural (e.g.,
  wrong cron schedule, missing setup step).
- **`CLAUDE.md`** — add project-specific guidance if the problem is about code conventions or
  patterns the bot keeps getting wrong.

**Prefer PRs over issues.** A PR with a clear description is immediately actionable.

- **PR** (default): Branch `daily/review-runs-$GITHUB_RUN_ID`, fix, commit, push, create with
  label `review-runs`. Put full analysis in PR description (run IDs, log excerpts, root cause,
  gate assessment).
- **Issue** (fallback): Only for problems too large or ambiguous to fix directly.

**Limit to at most 2 PRs per run.** Pick the highest-confidence findings; note the rest in the
tracking issue.

## Step 6: Summary

If no problems found (or none passed the gates), report "all clear" with: runs analyzed, sessions
reviewed, brief quality assessment, and any below-threshold findings recorded in the tracking
issue.
