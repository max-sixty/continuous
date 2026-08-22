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

After analysis, find **this skill's own evidence comment** on the tracking issue and **append** new findings to it. If it doesn't exist yet, create one. This avoids notification spam from frequent runs.

The guard must run **before any posting path** — append-existing and create-new both publish a comment that needs to embed the real run ID, and a guard placed inside one branch silently no-ops on the other. The first run after a monthly tracking issue is created always takes the create-new branch, so the guard belongs above the branch:

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
BOT_LOGIN=$(gh api user --jq '.login')
# Match the evidence log by its `## Run <id>` heading, not by "newest bot
# comment" — other skills (nightly) post their own comments on this issue, and
# the newest one is often not the log. Anchor with `(^|\n)`, not `^`: jq's `^`
# matches the start of the *string*, so a log comment that opens with a blank
# line or a `---` separator — the ordinary shape of the comment the create-new
# branch below posts — never matches, and the selector silently falls back to a
# superseded log comment. jq's `m` flag doesn't help; there it means "`.`
# matches newline", not multi-line anchors.
EXISTING_COMMENT=$(gh api "repos/$REPO/issues/$TRACKING_NUMBER/comments" \
  --jq "[.[] | select(.user.login == \"$BOT_LOGIN\" and (.body | test(\"(^|\n)## Run [0-9]\")))] | last | .id // empty")

# Verify the run heading references this run's $GITHUB_RUN_ID literally —
# fabricated round numbers produce dead Workflow links, see @review-gates.md.
# Unconditional: the create-new branch below also publishes a comment.
grep -qF "$GITHUB_RUN_ID" /tmp/findings.md || {
  echo "ERROR: /tmp/findings.md does not contain \$GITHUB_RUN_ID=$GITHUB_RUN_ID — refusing to post" >&2
  exit 1
}

if [ -n "$EXISTING_COMMENT" ]; then
  # Append only if the *combined* body fits. GitHub rejects bodies over 65536
  # characters and the PATCH has no fallback, so a 422 loses the leg's findings
  # while the run still reports success — size what you are about to POST, not
  # the existing comment. `wc -c` counts bytes, which is >= the character
  # count, so 60000 is a conservative bound.
  gh api "repos/$REPO/issues/comments/$EXISTING_COMMENT" --jq '.body' > /tmp/existing.md
  cat /tmp/existing.md /tmp/findings.md > /tmp/combined.md
  if [ "$(wc -c < /tmp/combined.md)" -lt 60000 ]; then
    gh api "repos/$REPO/issues/comments/$EXISTING_COMMENT" -X PATCH -F body=@/tmp/combined.md
  else
    gh api "repos/$REPO/issues/$TRACKING_NUMBER/comments" -F body=@/tmp/findings.md
  fi
else
  # No prior evidence-log comment on this month's tracking issue — create the
  # first one. Other bot comments may exist; they aren't append targets.
  gh api "repos/$REPO/issues/$TRACKING_NUMBER/comments" -F body=@/tmp/findings.md
fi
```

Never replace the body — prior entries contain per-run evidence needed for gate evaluation. See the finding format in `@review-gates.md`.

## Step 1: Find recent runs

List tend CI runs that completed since the previous `review-runs` run (nominally 24 hours — the cron runs daily):

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
# Anchor on the predecessor's start: a `date -d '24 hours ago'` resolves when
# the agent runs it, so the window opens after the predecessor started and
# drops the band in between. Steps 2 and 4 re-read the anchor from the file
# written below; shell variables don't survive between Bash tool calls.
#
# Derive the workflow id from this run rather than assuming the file name;
# exclude this run, because a re-run attempt of it can already read as
# completed and anchoring on itself collapses the window to zero.
# `status=success` reaches past a predecessor that died before its census, so
# that band still gets covered. Clamp a stale or missing anchor (an outage, or
# a fresh repo with no predecessor) so the window can recover one skipped day
# without pulling in a week — Step 5 dedups whatever a widened window sees
# twice.
WF_ID=$(gh api "repos/$REPO/actions/runs/$GITHUB_RUN_ID" --jq '.workflow_id')
PREV_START=$(gh api "repos/$REPO/actions/workflows/$WF_ID/runs?status=success&per_page=10" \
  --jq "[.workflow_runs[] | select(.id != ${GITHUB_RUN_ID:-0}) | .created_at] | max // empty")
SINCE=${PREV_START:-$(date -u -d '25 hours ago' +%Y-%m-%dT%H:%M:%SZ)}
FLOOR=$(date -u -d '49 hours ago' +%Y-%m-%dT%H:%M:%SZ)
if [[ "$SINCE" < "$FLOOR" ]]; then SINCE=$FLOOR; fi
echo "$SINCE" > /tmp/review-runs-since
# Add the repo's extra prefixes from its `running-tend` skill: any workflow
# running the tend action is in scope, not just the generated `tend-*` ones.
# Step 2 prices the same list.
#
# `--paginate` on both calls. Both endpoints page at 30 by default and return
# runs newest-first, so without it the census silently covers only the most
# recent 30 runs per workflow — on a busy repo that is the last hour, not the
# last 24. Each `--jq` here is a per-element projection, so `--paginate`
# applying it per page is harmless.
PREFIXES=("tend-")
PREFIX_RE="^($(IFS='|'; echo "${PREFIXES[*]}"))"
# Census on *completion*, which is the axis that tiles. A run created before
# `$SINCE` may still have been in progress at the predecessor's census, so
# `status=completed` dropped it there — filtering on `created` here would drop
# it again and nobody would ever see it, and those are the long-running runs
# Step 3 goes on to hunt. So over-fetch by `created` and filter on
# `updated_at`, as `list-recent-runs.sh` does. The floor is a run's whole
# lifetime, not its job cap: `created_at` starts at queue time, and a
# `cancel-in-progress: false` group can hold a run queued for many hours
# before its 6h of execution even begins.
FETCH_FROM=$(date -u -d "$SINCE - 24 hours" +%Y-%m-%dT%H:%M:%SZ)
for workflow in $(gh api --paginate repos/$REPO/actions/workflows --jq ".workflows[] | select(.name | test(\"$PREFIX_RE\")) | .id"); do
  gh api --paginate "repos/$REPO/actions/workflows/$workflow/runs?created=>=$FETCH_FROM&status=completed&per_page=100" \
    --jq ".workflow_runs[] | select(.updated_at >= \"$SINCE\") | {databaseId: .id, conclusion, createdAt: .created_at, updatedAt: .updated_at, name: .name}"
done
```

If no runs found, report "no runs to review" and exit.

Report the run census as the count this returns. `.total_count` counts the wider `FETCH_FROM` fetch, so it bounds the census from above rather than matching it — but a census that lands on a round page boundary (30, 100) is still the signature of a page that was never followed, so check that one against `.total_count` before trusting it.

Then, for each run ID from above, pull its jobs and classify them:

- **Long-running** (>30 min): Tend runs typically finish in single-digit minutes. Anything over 30 is worth a look — download session logs in Step 3 and diagnose where the time went (long background waits, push-wait-fix cycles, a stuck tool call).
- **Near-timeout** (within 90% of the cap): A job that consumed most of its timeout budget is one slow external check away from being killed. Structural, but classify the cost per Gate 3 by what the kill left on the record: usually waste-class (a cron-driven run a later tick retries), except where the killed session had already taken an outward action it was still gated on — a `tend-review` job killed mid-poll leaves its approval standing over red CI. Waste-class gets only a remedy that passes Gate 3, or nothing.

To determine the timeout cap for a workflow, read `timeout-minutes` from that workflow's own file under `.github/workflows/` — the census admits workflows named outside the `tend-` prefix, so don't glob for one. Tend's generated workflows do not set `timeout-minutes`, so GitHub's 360-minute default applies unless the adopter has overridden it via `workflows.<name>.jobs.<job>.timeout-minutes` in `.config/tend.yaml`.

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

A run that fails files a row on a `tend-outage`-labelled **"Bot temporarily unavailable"** issue, naming the run and the trigger it stranded. Nothing re-runs those triggers — `tend-review` fires only on `pull_request_target`, so a PR whose one review attempt died stays unreviewed until someone pushes. Drain the open issue as part of this sweep.

```bash
# Usually empty; no open outage issue means nothing stranded.
OUTAGE=$(gh issue list --state open --label tend-outage --json number,title \
  --jq '[.[] | select(.title == "Bot temporarily unavailable")][0].number // empty')
gh issue view "$OUTAGE" --json body,comments --jq '.body, .comments[].body' \
  | grep -oE 'runs/[0-9]+|\| #[0-9]+'
```

**Diagnose first.** The nightly enrichment comment names the cause when it can. When it doesn't, read the session log — quota exhaustion surfaces as a `<synthetic>` assistant message:

```bash
gh run download <run-id> --pattern '*session-logs*' --dir /tmp/outage
jq -r 'select(.type == "assistant") | .message.content[]?.text // empty' /tmp/outage/*/*/*.jsonl
# → You've hit your weekly limit · resets 12am (UTC)
```

A cluster of these is quota exhaustion, not a bug — don't open a fix PR, and read the reset off the message: a weekly limit can strand most of a day.

**Re-run only what won't recover.** Scheduled workflows (`nightly`, `notifications`, `weekly`, this one) recover on their next tick. For an event-triggered run, confirm the work is still missing — a later push often re-triggers it — and that a recent run completed cleanly, or the re-run just refills the issue. Re-running the bot's own failed workflow needs no maintainer approval.

```bash
gh pr view <n> --json state,headRefOid,reviews \
  --jq '{state, headRefOid, reviewers: [.reviews[].author.login]}'
gh run rerun <run-id> --failed
```

Close the issue once every row is drained (`gh issue close "$OUTAGE"`); one left open folds the next outage into a stale incident.

## Step 2: Token usage report

Run the token report script to get per-run token counts:

```bash
# Whole hours back to Step 1's anchor, rounded up so the whole band is priced.
# A literal `24` reopens the gap Step 1 closed. The `cat` isn't optional: an
# unset `$SINCE` makes `date -d ""` today's midnight, not an error.
SINCE=$(cat /tmp/review-runs-since)
HOURS=$(( ( $(date -u +%s) - $(date -u -d "$SINCE" +%s) + 3599 ) / 3600 ))
"${CLAUDE_PLUGIN_ROOT}/scripts/token-report.sh" "$HOURS" > /tmp/token-report.json
```

Pass the same extra prefixes Step 1 censuses (after `$HOURS`, which the script reads as its first positional arg), so the two steps agree on what the fleet is — the repo's `running-tend` skill is the source for both (e.g. `review-` for a `review-reviewers` workflow that uses the tend action but isn't named `tend-*`).

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

Dispositions — merged, closed, relabeled, reverted — are only half the signal. A maintainer replying in-thread that a bot claim was wrong, or requesting changes on a bot PR, leaves labels and state untouched and is equally a correction; where the bot authors most of the PRs, a review body is the *first* place a maintainer writes. The script collects all three — dispositions, thread comments, review bodies — for the window:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/review-runs-corrections.sh" "$(cat /tmp/review-runs-since)"
```

Read every row: a correction is a maintainer contradicting a bot claim, not merely replying. Comment rows carry both timestamps because the window filters on `updated_at` — a `created` before the anchor is an older comment edited inside the window, a real hit rather than a broken filter. Empty `dispositions`, `comments`, and `reviews` is the all-clear.

Write "no maintainer corrections" into the tracking issue only after the script ran and returned empty — future runs read the phrase as ground truth when counting occurrences under Gate 1, so an unchecked all-clear suppresses the evidence it exists to accumulate. The script exits non-zero rather than reporting an empty window when the anchor or the bot login is missing, since both filters fail open.

## Step 5: Deduplicate

Before creating issues or PRs, check for existing ones:

```bash
gh issue list --state open --limit 200 --json number,title,body
gh issue list --state closed --json number,title,closedAt --limit 30
# --state all: a merged PR is the most common way a finding is already fixed
gh pr list --state all --limit 40 --json number,title,state,mergedAt,headRefName,body
# Bundled-skill defects are filed upstream (Step 6), and the queries above only
# see this repo — dedup against tend before filing there.
gh pr list --repo max-sixty/tend --state all --limit 40 --json number,title,state,mergedAt,body
gh issue list --repo max-sixty/tend --state all --limit 40 --json number,title,body
```

Search titles AND bodies for related keywords.

**A fix merged upstream still reproduces here.** The action ref is pinned per release, so a skill fix that merged in `max-sixty/tend` stays dormant on this repo until the next release tags. Observing the bug is therefore not evidence the fix is missing — check tend's merged PRs before filing, or the report is churn on something already landed.

## Step 6: Act on findings

Improvements target **repo-local** files by default:

- **`.claude/skills/`** — update or create skill overlays with guidance that prevents the identified problem. Prefer updating existing skill files over creating new ones.
- **`.config/tend.yaml`** — adjust workflow configuration if the problem is structural (e.g., wrong cron schedule, missing setup step).
- **`CLAUDE.md`** — add project-specific guidance if the problem is about code conventions or patterns the bot keeps getting wrong.

**Bundled-skill defects.** If the root cause is a gap or bug in a bundled skill (`plugins/tend-ci-runner/skills/...` in `max-sixty/tend`) — the same pattern would fire in every consumer — file the fix against tend per **Other Repos** in `running-in-ci`. Signal: the fix reads as generic guidance that would apply to any consumer.

**Prefer PRs over issues.** A PR with a clear description is immediately actionable.

Editing `.claude/skills/` requires the read-only-mount workaround (bind-mounted read-only, plus a harness write-guard on `.claude/skills/` paths) — see `references/skill-pr-workflow.md` in `/tend-ci-runner:running-in-ci`. Adapted for review-runs (base on `HEAD` since this runs on a schedule, not a PR checkout; move each edited file into place):


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

- **PR** (default): Branch `daily/review-runs-$GITHUB_RUN_ID`, fix, commit, push, create with label `review-runs`. Lead the PR description with two or three sentences — problem, fix, verification — and put the full analysis (run IDs, log excerpts, root cause, gate assessment) inside `<details>`.
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
