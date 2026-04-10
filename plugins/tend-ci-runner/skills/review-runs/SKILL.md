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

List Claude CI runs that completed in the past 24 hours (the cron runs daily):

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
SINCE=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
for workflow in $(gh api repos/$REPO/actions/workflows --jq '.workflows[] | select(.name | startswith("tend-")) | .id'); do
  gh api "repos/$REPO/actions/workflows/$workflow/runs?created=>=$SINCE&status=completed" \
    --jq '.workflow_runs[] | {databaseId: .id, conclusion, createdAt: .created_at, name: .name}'
done
```

If no runs found, report "no runs to review" and exit.

Then, for each run ID from above, pull its jobs and classify them:

- **Long-running** (>30 min): Tend runs typically finish in single-digit minutes. Anything over 30 is worth
  a look — download session logs in Step 3 and diagnose where the time went (long background waits,
  push-wait-fix cycles, a stuck tool call).
- **Near-timeout** (within 90% of the cap): A job that consumed most of its timeout budget is one slow
  external check away from being killed. These are **structural** failures: one occurrence is enough to act on.

To determine the timeout cap for a workflow, read `timeout-minutes` from the workflow YAML file
(`.github/workflows/tend-*.yaml`). Tend's generated workflows do not set `timeout-minutes`, so GitHub's
360-minute default applies unless the adopter has overridden it via `[workflows.<name>.jobs.<job>.timeout-minutes]`
in `.config/tend.toml`.

```bash
# Flag long-running and near-timeout jobs
gh api "repos/$REPO/actions/runs/$RUN_ID/jobs" \
  --jq '.jobs[]
    | ((.completed_at | fromdateiso8601) - (.started_at | fromdateiso8601)) as $dur
    | select($dur >= 1800)   # 30 min
    | {name, conclusion, duration_min: ($dur / 60 | floor), url: .html_url}'
```

After retrieving the timeout cap from the workflow file, flag any job whose duration exceeded 90% of it as a
near-timeout. For the default 360-min cap, that threshold is 324 min.

## Step 2: Token usage report

Run the token report script to get per-run token counts:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/token-report.sh" 24 > /tmp/token-report.json
```

Include the totals and per-workflow breakdown in the summary (Step 6). Flag any
runs with unusually high token usage for closer inspection in Step 3.

## Step 3: Download and analyze session logs

Load `/install-tend:debug-ci-session` for download commands and JSONL parsing queries.

Skip runs without artifacts. Trace decision chains: what did Claude decide, what evidence did it
use, what was the outcome?

## Step 4: Cross-check outcomes

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

## Step 5: Deduplicate

Before creating issues or PRs, check for existing ones:

```bash
gh issue list --state open --json number,title,body
gh pr list --state open --json number,title,headRefName,body
gh issue list --state closed --json number,title,closedAt --limit 30
```

Search titles AND bodies for related keywords.

## Step 6: Act on findings

Improvements target **repo-local** files:

- **`.claude/skills/`** — update or create skill overlays with guidance that prevents the
  identified problem. Prefer updating existing skill files over creating new ones.
- **`.config/tend.toml`** — adjust workflow configuration if the problem is structural (e.g.,
  wrong cron schedule, missing setup step).
- **`CLAUDE.md`** — add project-specific guidance if the problem is about code conventions or
  patterns the bot keeps getting wrong.

**Prefer PRs over issues.** A PR with a clear description is immediately actionable.

The checkout's `.claude/` directory is bind-mounted read-only under the sandbox
(protecting bots from modifying their own skills in place), so edits to
`.claude/skills/` files fail with `OSError: [Errno 30] Read-only file system`.
Do the edit, commit, and push from a git worktree under `$TMPDIR`, which is
writable:

```bash
git worktree add "$TMPDIR/review-runs-fix" -b daily/review-runs-$GITHUB_RUN_ID HEAD
cd "$TMPDIR/review-runs-fix"
# edit .claude/skills/... here
git add .claude/skills/...
git commit -m "skills(running-tend): ..."
git push -u origin daily/review-runs-$GITHUB_RUN_ID
gh pr create --title "..." --body-file /tmp/pr-body.md --head daily/review-runs-$GITHUB_RUN_ID
cd -
git worktree remove "$TMPDIR/review-runs-fix" --force
```

`.config/tend.toml` and `CLAUDE.md` are not under the read-only mount, but if
you're already in the worktree for a `.claude/skills/` edit, do those edits
there too so the branch stays self-contained.

- **PR** (default): Branch `daily/review-runs-$GITHUB_RUN_ID`, fix, commit, push, create with
  label `review-runs`. Put full analysis in PR description (run IDs, log excerpts, root cause,
  gate assessment).
- **Issue** (fallback): Only for problems too large or ambiguous to fix directly.

**Limit to at most 2 PRs per run.** Pick the highest-confidence findings; note the rest in the
tracking issue.

## Step 7: Summary

If no problems found (or none passed the gates), report "all clear" with: runs analyzed, sessions
reviewed, brief quality assessment, and any below-threshold findings recorded in the tracking
issue.
