---
name: review-runs
description: Daily review of the previous night's CI runs — identifies problems and improves repo-local skills and workflows.
metadata:
  internal: true
---

# Review Runs

Analyze the previous night's tend CI runs in this repository. Identify behavioral problems, skill gaps, and workflow issues — then propose improvements to the repo's local skills and workflows.

This skill runs **in the adopter repo**, not in tend. Improvements target `.claude/skills/` and `.config/tend.yaml` in this repository.

## First steps

Load `/tend-ci-runner:running-in-ci` first — it contains CI security rules, PR/comment formatting (line wrapping, heredoc hazards), and polling conventions. This skill opens PRs and issue comments, so those rules apply.

```bash
ls .claude/skills/
```

Load any repo-specific skill overlay before proceeding.

@review-gates.md

## Evidence accumulation

Each run only sees a window of CI sessions, but patterns emerge over days or weeks. Accumulate evidence in a **monthly tracking issue** labeled `review-runs-tracking`.

<!-- TODO: migrate this to gist-backed storage once the review-reviewers pilot validates it -->

### Finding or creating the tracking issue

`gh issue create` prints the new issue's URL; parse the number from its basename. Sort and pick the lowest-numbered match so later runs stay deterministic if the month ever has duplicate tracking issues.

```bash
MONTH=$(date +%Y-%m)
TRACKING_LABEL="review-runs-tracking"
TRACKING_NUMBER=$(gh issue list --state open --label "$TRACKING_LABEL" \
  --json number,title --jq ".[] | select(.title | contains(\"$MONTH\")) | .number" \
  | sort -n | head -1)

if [ -z "$TRACKING_NUMBER" ]; then
  cat > /tmp/tracking-body.md << 'EOF'
Monthly tracking issue for below-threshold findings. Each run appends findings as a comment. Future runs read these to build cumulative evidence.

**Do not close manually** — a new issue is created each month, and prior months are closed automatically.
EOF
  TRACKING_URL=$(gh issue create \
    --title "$TRACKING_LABEL: $MONTH" \
    --label "$TRACKING_LABEL" \
    -F /tmp/tracking-body.md)
  if [ -z "$TRACKING_URL" ]; then
    echo "ERROR: gh issue create failed" >&2
    exit 1
  fi
  TRACKING_NUMBER=$(basename "$TRACKING_URL")
fi
```

### Closing prior-month tracking issues

Once a new month's issue exists, close any open tracking issues from earlier months. Run this unconditionally — it's a no-op when nothing's stale, and self-heals if a previous run failed to close.

```bash
gh issue list --state open --label "$TRACKING_LABEL" \
  --json number,title --jq ".[] | select(.title | contains(\"$MONTH\") | not) | .number" \
  | while read -r OLD; do
      gh issue close "$OLD" --comment "Superseded by #$TRACKING_NUMBER ($MONTH)."
    done
```

### Reading historical evidence

Before applying the gates, read the current tracking issue's comments to find prior observations that overlap with current findings:

```bash
gh issue view "$TRACKING_NUMBER" --json comments \
  --jq '.comments[] | {author: .author.login, body: .body}'
```

Also check last month's tracking issue (if it exists) for recent carry-over.

### Recording below-threshold findings

After analysis, find **the bot's existing comment** on the tracking issue and **append** new findings to it. If no bot comment exists yet, create one. This avoids notification spam from frequent runs.

The guard must run **before any posting path** — append-existing and create-new both publish a comment that needs to embed the real run ID, and a guard placed inside one branch silently no-ops on the other. The first run after a monthly tracking issue is created always takes the create-new branch, so the guard belongs above the branch:

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
BOT_LOGIN=$(gh api user --jq '.login')
EXISTING_COMMENT=$(gh api "repos/$REPO/issues/$TRACKING_NUMBER/comments" \
  --jq "[.[] | select(.user.login == \"$BOT_LOGIN\")] | last | .id // empty")

# Verify the run heading references this run's $GITHUB_RUN_ID literally —
# fabricated round numbers produce dead Workflow links, see @review-gates.md.
# Unconditional: the create-new branch below also publishes a comment.
grep -qF "$GITHUB_RUN_ID" /tmp/findings.md || {
  echo "ERROR: /tmp/findings.md does not contain \$GITHUB_RUN_ID=$GITHUB_RUN_ID — refusing to post" >&2
  exit 1
}

if [ -n "$EXISTING_COMMENT" ]; then
  # Append to existing comment if it fits. GitHub rejects bodies over 65536
  # characters — start a new comment when the existing one is too large.
  gh api "repos/$REPO/issues/comments/$EXISTING_COMMENT" --jq '.body' > /tmp/existing.md
  EXISTING_SIZE=$(wc -c < /tmp/existing.md)
  if [ "$EXISTING_SIZE" -lt 50000 ]; then
    cat /tmp/existing.md /tmp/findings.md > /tmp/combined.md
    gh api "repos/$REPO/issues/comments/$EXISTING_COMMENT" -X PATCH -F body=@/tmp/combined.md
  else
    gh api "repos/$REPO/issues/$TRACKING_NUMBER/comments" -F body=@/tmp/findings.md
  fi
else
  # No prior bot comment on this month's tracking issue — create the first one.
  gh api "repos/$REPO/issues/$TRACKING_NUMBER/comments" -F body=@/tmp/findings.md
fi
```

Never replace the body — prior entries contain per-run evidence needed for gate evaluation. See the finding format in `@review-gates.md`.

## Step 1: Find recent runs

List tend CI runs that completed in the past 24 hours (the cron runs daily):

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

- **Long-running** (>30 min): Tend runs typically finish in single-digit minutes. Anything over 30 is worth a look — download session logs in Step 3 and diagnose where the time went (long background waits, push-wait-fix cycles, a stuck tool call).
- **Near-timeout** (within 90% of the cap): A job that consumed most of its timeout budget is one slow external check away from being killed. These are **structural** failures: one occurrence is enough to act on.

To determine the timeout cap for a workflow, read `timeout-minutes` from the workflow YAML file (`.github/workflows/tend-*.yaml`). Tend's generated workflows do not set `timeout-minutes`, so GitHub's 360-minute default applies unless the adopter has overridden it via `workflows.<name>.jobs.<job>.timeout-minutes` in `.config/tend.yaml`.

```bash
# Flag long-running and near-timeout jobs
gh api "repos/$REPO/actions/runs/$RUN_ID/jobs" \
  --jq '.jobs[]
    | ((.completed_at | fromdateiso8601) - (.started_at | fromdateiso8601)) as $dur
    | select($dur >= 1800)   # 30 min
    | {name, conclusion, duration_min: ($dur / 60 | floor), url: .html_url}'
```

After retrieving the timeout cap from the workflow file, flag any job whose duration exceeded 90% of it as a near-timeout. For the default 360-min cap, that threshold is 324 min.

### Drain stranded triggers

A run whose agent session exits non-zero files (or appends to) a `tend-outage`-labelled **"Bot temporarily unavailable"** issue — one row per failure, naming the run and the trigger it stranded. Nothing re-runs those triggers: `tend-review` fires only on `pull_request_target`, so a PR whose one review attempt died stays unreviewed until someone pushes again. Drain the open outage issue as part of this sweep.

```bash
# Empty on most days — no open outage issue means nothing stranded; skip the rest.
OUTAGE=$(gh issue list --state open --label tend-outage --json number --jq '.[0].number // empty')
gh issue view "$OUTAGE" --json body,comments --jq '.body, .comments[].body' \
  | grep -oE 'runs/[0-9]+|\| #[0-9]+'
```

**Diagnose before re-running.** The issue body says only "The bot failed to process a request"; the failure annotation, which the nightly enrichment pass carries into the issue as a comment, is the cheapest next look but doesn't always name the cause. When it doesn't, read the session log — a subscription-quota exhaustion surfaces as a `<synthetic>` assistant message:

```bash
gh run download <run-id> --pattern '*session-logs*' --dir /tmp/outage
jq -r 'select(.type == "assistant") | .message.content[]?.text // empty' /tmp/outage/*/*/*.jsonl
# → You've hit your session limit · resets 8:30am (UTC)
# → You've hit your weekly limit · resets 12am (UTC)
```

A cluster of these is quota exhaustion, not a bug — don't open a fix PR. The session and weekly limits reset on different clocks, so read the reset off the message rather than assuming the shorter session window; a weekly exhaustion can strand most of a day.

**Re-run only what won't recover on its own.** Scheduled workflows (`nightly`, `notifications`, `weekly`, this one) recover on their next cron tick — re-running them double-spends quota. For an event-triggered run (`review`, `mention`, `triage`, `ci-fix`), confirm the work is still missing first; a later push often re-triggers the workflow on its own, and re-running a job that already happened burns quota for nothing:

```bash
gh pr view <n> --json state,headRefOid,reviews \
  --jq '{state, headRefOid, reviewers: [.reviews[].author.login]}'
gh run rerun <run-id> --failed
```

Re-run only once a later run has completed cleanly — re-running into a still-exhausted quota just refills the outage issue. Re-running the bot's own failed workflow is restorative, not destructive; no maintainer approval needed. Close the issue once every row is drained (`gh issue close "$OUTAGE"`): the harness auto-closes only duplicates from a create-create race, never the surviving issue, so one left open makes tomorrow's sweep re-check the same rows and folds the next outage into a stale incident.

## Step 2: Token usage report

Run the token report script to get per-run token counts:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/token-report.sh" 24 > /tmp/token-report.json
```

Pass additional workflow prefixes to include non-`tend-*` workflows that use the tend action (e.g., `review-reviewers`). Check the repo's `running-tend` skill for the list.

Include the totals and per-workflow breakdown in the summary (Step 7). Flag any runs with unusually high token usage for closer inspection in Step 3.

## Step 3: Download and analyze session logs

Load `/install-tend:debug-tend-run` for download commands and JSONL parsing queries.

Skip runs without artifacts. Trace decision chains: what did tend decide, what evidence did it use, what was the outcome?

## Step 4: Cross-check outcomes

For each analyzed run, compare what the bot did against what happened next. The same "did it stick?" question applies to every tend workflow — ask it of whatever ran. For example:

- **Review**: did subsequent commits undo something the bot approved? Did human reviewers flag issues it missed?
- **Triage**: was the classification correct? Did the issue get relabeled?
- **Nightly**: did the bot's PRs merge, or get closed as unhelpful?
- **CI-fix**: did the fix actually resolve the failure?

mention, notifications, weekly, and review-reviewers runs get the same treatment: find the bot's output and check whether it was accepted.

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

Improvements target **repo-local** files by default:

- **`.claude/skills/`** — update or create skill overlays with guidance that prevents the identified problem. Prefer updating existing skill files over creating new ones.
- **`.config/tend.yaml`** — adjust workflow configuration if the problem is structural (e.g., wrong cron schedule, missing setup step).
- **`CLAUDE.md`** — add project-specific guidance if the problem is about code conventions or patterns the bot keeps getting wrong.

**Bundled-skill defects.** If the root cause is a gap or bug in a bundled skill (`plugins/tend-ci-runner/skills/...` in `max-sixty/tend`) — the same pattern would fire in every consumer — file the fix against tend per **Filing Issues in Other Repos** in `running-in-ci`. Signal: the fix reads as generic guidance that would apply to any consumer.

**Prefer PRs over issues.** A PR with a clear description is immediately actionable.

Editing `.claude/skills/` requires the read-only-mount workaround (bind-mounted read-only, plus a harness write-guard on `.claude/skills/` paths) — see **Learning from Feedback** in `/tend-ci-runner:running-in-ci`. Adapted for review-runs (base on `HEAD` since this runs on a schedule, not a PR checkout; move each edited file into place):


```bash
git worktree add "/tmp/review-runs-fix" -b daily/review-runs-$GITHUB_RUN_ID HEAD

# Use the Write tool to author each edited skill file to /tmp/<name>.md.
# Then move the files into place:
cd "/tmp/review-runs-fix/.claude/skills/running-tend" && mv /tmp/running-tend.md SKILL.md
# Repeat per skill file being updated.

cd "/tmp/review-runs-fix"
git add .claude/skills/
# Set git identity first if not already done this session — a fresh worktree has
# none and the commit fails with `Author identity unknown`. See "Configure git
# identity before the first commit" in /tend-ci-runner:running-in-ci.
git commit -m "skills(running-tend): ..."
git push -u origin daily/review-runs-$GITHUB_RUN_ID
gh pr create --title "..." --body-file /tmp/pr-body.md --head daily/review-runs-$GITHUB_RUN_ID
cd -
git worktree remove "/tmp/review-runs-fix" --force
```

`.config/tend.yaml` and `CLAUDE.md` are not under the read-only mount, but if you're already in the worktree for a `.claude/skills/` edit, do those edits there too so the branch stays self-contained.

- **PR** (default): Branch `daily/review-runs-$GITHUB_RUN_ID`, fix, commit, push, create with label `review-runs`. Put full analysis in PR description (run IDs, log excerpts, root cause, gate assessment).
- **Issue** (fallback): Only for problems too large or ambiguous to fix directly.

**Limit to at most 2 PRs per run.** Pick the highest-confidence findings; note the rest in the tracking issue.

## Step 7: Summary

If no problems found (or none passed the gates), report "all clear" with: runs analyzed, sessions reviewed, brief quality assessment, and any below-threshold findings recorded in the tracking issue.

Save the summary to `/tmp/claude/step-summary.md` (a post-Claude step copies this into the GitHub Actions step summary):

```bash
mkdir -p /tmp/claude
cat > /tmp/claude/step-summary.md << 'EOF'
## Review-runs summary
...
EOF
```
