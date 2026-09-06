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

Each run contributes to a monthly `review-runs-tracking` issue. Prepare the
current tracker and read the current and previous month's evidence:

```bash
uv run --script \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_runs.py" prepare-evidence
```

The command creates this month's tracker when needed, closes older open
trackers, persists the current issue id, and prints both evidence windows.

After analysis, write the new findings in the format from `@review-gates.md`
to `/tmp/findings.md`. Include a literal `## Run $GITHUB_RUN_ID` heading.
Then append them:

```bash
uv run --script \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_runs.py" append-evidence
```

The command refuses a findings file that does not name this run. It appends to
the latest bot evidence comment while the combined body remains below 60 KB,
then starts a new comment. Prior entries are never replaced.

## Step 1: Find recent runs

List tend CI runs that completed since the previous `review-runs` run (nominally 24 hours — the cron runs daily):

```bash
uv run --script \
  "${CLAUDE_PLUGIN_ROOT}/scripts/list_recent_runs.py" review-runs
```

If no runs are found, report "no runs to review", complete **Reconcile live work** below, then exit.

Report the run census as the count this returns. `.total_count` counts the wider `FETCH_FROM` fetch, so it bounds the census from above rather than matching it — but a census that lands on a round page boundary (30, 100) is still the signature of a page that was never followed, so check that one against `.total_count` before trusting it.

Then, for each run ID from above, pull its jobs and classify them:

- **Long-running** (>30 min): Tend runs typically finish in single-digit minutes. Anything over 30 is worth a look — download session logs in Step 3 and diagnose where the time went (long background waits, push-wait-fix cycles, a stuck tool call).
- **Near-timeout** (within 90% of the cap): A job that consumed most of its timeout budget is one slow external check away from being killed. Structural, but classify the cost per Gate 3 by what the kill left on the record: usually waste-class (a cron-driven run a later tick retries), except where the killed session had already taken an outward action it was still gated on — a `tend-review` job killed mid-poll leaves its approval standing over red CI. Waste-class gets only a remedy that passes Gate 3, or nothing.

To determine the timeout cap for a workflow, read `timeout-minutes` from that workflow's own file under `.github/workflows/` — the census admits workflows named outside the `tend-` prefix, so don't glob for one. Tend's generated workflows do not set `timeout-minutes`, so GitHub's 360-minute default applies unless the adopter has overridden it via `workflows.<name>.jobs.<job>.timeout-minutes` in `.config/tend.yaml`.

```bash
# Flag long-running and near-timeout jobs
gh api "repos/$GITHUB_REPOSITORY/actions/runs/$RUN_ID/jobs" \
  --jq '.jobs[]
    | ((.completed_at | fromdateiso8601) - (.started_at | fromdateiso8601)) as $dur
    | select($dur >= 1800)   # 30 min
    | {name, conclusion, duration_min: ($dur / 60 | floor), url: .html_url}'
```

After retrieving the timeout cap from the workflow file, flag any job whose duration exceeded 90% of it as a near-timeout. For the default 360-min cap, that threshold is 324 min.

### Reconcile live work

The failed-run census and the `tend-outage` issue diagnose availability. Do not replay historical workflow runs to recover their event payloads: issue and PR recovery belongs to the unread notification queue, which applies the current workflow and current repository state.

As a daily backstop for delayed notifications, retention, edited activity, and repaired subscriptions, inspect the live repository for:

- an open issue with no bot response to the latest human activity;
- an open PR whose live head has no bot review, or whose latest comment, review, or inline review comment directed at the bot has no response; this includes replies to the bot's review on a fork PR;
- failing default-branch CI with no bot fix in progress.

Handle live work through the normal triage, review, or CI-fix guidance. Keep
failed runs in the report as diagnostic evidence.

After the exhaustive live scan, find the canonical current outage tracker and
read every row. The issue body holds the first row and later rows are comments.
Fail the sweep if the lookup fails; that is different from finding no open
tracker:

```bash
if ! gh issue list --state open --label tend-outage --author @me \
  --limit 100 --json number,title \
  --jq '[.[] | select(.title == "Bot temporarily unavailable") | .number]
    | sort | .[0] // empty' > /tmp/review-runs-outage-number; then
  echo "Could not read the outage tracker" >&2
  exit 1
fi
OUTAGE=$(cat /tmp/review-runs-outage-number)
if [ -n "$OUTAGE" ]; then
  gh issue view "$OUTAGE" --json body,comments --jq '.body, .comments[].body'
fi
```

Use every row to identify what the failed run may have missed. Diagnose it and
handle any applicable current work. If a tracker was found, close the exact
issue number returned above:

```bash
OUTAGE=$(cat /tmp/review-runs-outage-number)
[ -n "$OUTAGE" ] && gh issue close "$OUTAGE" --reason completed
```

## Step 2: Token usage report

Run the token report script to get per-run token counts:

```bash
# Whole hours back to Step 1's anchor, rounded up so the whole band is priced.
# A literal `24` reopens the gap Step 1 closed. The `cat` isn't optional: an
# unset `$SINCE` makes `date -d ""` today's midnight, not an error.
SINCE=$(cat /tmp/review-runs-since)
HOURS=$(( ( $(date -u +%s) - $(date -u -d "$SINCE" +%s) + 3599 ) / 3600 ))
uv run --script \
  "${CLAUDE_PLUGIN_ROOT}/scripts/token_report.py" "$HOURS" \
  > /tmp/token-report.json
```

Pass the same extra prefixes Step 1 censuses (after `$HOURS`, which the script reads as its first positional arg), so the two steps agree on what the fleet is — the repo's `running-tend` skill is the source for both (e.g. `review-` for a `review-reviewers` workflow that uses the tend action but isn't named `tend-*`).

Include the total cost and the per-workflow breakdown in the summary (Step 7). Escalate outliers to Step 3 — for example a run far above its workflow's usual cost, or a subject the subject table shows several runs against.

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
uv run --script \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_runs_corrections.py" \
  "$(cat /tmp/review-runs-since)"
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

- **PR** (default): Branch `daily/review-runs-$GITHUB_RUN_ID`, fix, commit, push, create with label `review-runs`. Write the description for a maintainer deciding whether the current change fixes the general behavior gap, following **Reader-facing prose** in `running-in-ci`. Link the tracking issue where it holds prior observations of the same behavior, and carry the evidence that justified promoting this finding — the run IDs, the log excerpt, and the gate assessment — in the body or a `<details>` block.
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
