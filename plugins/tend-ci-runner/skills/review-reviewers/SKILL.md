---
name: review-reviewers
description: Hourly outcome-based analysis of tend's CI behavior — checks whether tend's outputs were accepted or rejected, escalating to session logs only when outcomes look wrong.
argument-hint: "<owner/repo>"
metadata:
  internal: true
---

# Review Reviewers

Analyze tend's CI behavior on the target repo over the past hour. Focus on **outcomes** — what the bot produced publicly and whether it was accepted — rather than internal session mechanics. Create PRs or issues on tend when outcomes reveal behavioral problems.

## First steps

Load `/tend-ci-runner:running-in-ci` first — it contains CI security rules, PR/comment formatting (line wrapping, heredoc hazards), and polling conventions. This skill opens PRs and issue comments on tend, so those rules apply.

## Cost discipline: cheap subagents for exploration

Session log parsing and outcome checking are token-heavy. Delegate all broad exploration to a **cheap subagent** (e.g. Haiku / gpt-mini). Keep the main agent for judgment: evaluating findings against gates, deciding whether to act, and drafting PRs.

Pattern:
1. Main agent sets up context (bot identity, repo guidance, run list)
2. Main agent spawns cheap subagent to survey outcomes across all runs → receives structured summary
3. Main agent evaluates the summary against gates
4. If needed, main agent spawns another cheap subagent to investigate specific session logs → receives diagnosis
5. Main agent drafts fix PR if warranted

## Core principle: outcomes over internals

The bot's job is to produce useful outputs: reviews, triage comments, fix commits, issue responses. The cheapest way to evaluate quality is to check whether those outputs were **accepted** (merged, kept, acted on) or **rejected** (reverted, closed, corrected, disagreed with).

Session logs are expensive to download and parse. Only escalate to session-log inspection when outcome signals indicate a real problem worth diagnosing.

## Core principle: repo-specific guidance is primary

Each adopter repo has its own guidance (`running-tend` skill or equivalent) that shapes how the bot should behave in that repo. This repo-specific guidance **takes precedence** over tend's default rules. The bot's job is to follow the repo-specific guidance first, falling back to tend's defaults only where the repo doesn't specify.

## Non-issues: do not flag these

Some patterns look suspicious but are intentional. Before drafting a finding, check this list — flagging expected behaviors creates maintainer churn and costs trust.

- **`tend-review` re-approving after the bot pushed a fix commit.** The reviewer role is independent of commit and PR authorship. Re-reviewing (and re-approving) after `tend-notifications`, `tend-ci-fix`, or a mention run pushes a fix is expected behavior, not a re-approval loop. Authorship-keyed guards (skipping re-review when the bot pushed) are not the accepted shape — do not propose them. If you observe stacked approvals from concurrent runs that raced with concurrency-group cancellation, that is a *concurrency* issue (the cancelled runs managed to POST before the SIGTERM arrived), not a review-rule problem.

- **`tend-review` exiting silent on a genuinely self-authored PR with no concerns.** The bundled `review` skill explicitly authorizes "stay silent and skip to step 6" on self-authored PRs (`PR_AUTHOR == BOT_LOGIN`) without findings ([`review/SKILL.md`](https://github.com/max-sixty/tend/blob/main/plugins/tend-ci-runner/skills/review/SKILL.md), "Self-authored PRs") — GitHub rejects self-approvals, so APPROVE isn't an option. Treat silent first-reviews on bot-authored PRs as expected behavior, not "silent omission" or "divergence". Before citing an "APPROVE-on-bot-authored-PR pattern" as a counter-precedent, verify each cited PR's literal author with `gh pr view <n> --json author --jq '.author.login'`: owner-authored PRs (e.g. `max-sixty`) are reviewed and approved normally and are not a "bot-authored APPROVE" precedent.

- **`tend-review` staying silent on a draft PR with no actionable findings.** GitHub's `state` field reports draft PRs as `OPEN`, so "no review on an open PR" reads as omission when it is expected behavior. The bundled `review` skill's draft-mode ([`review/SKILL.md`](https://github.com/max-sixty/tend/blob/main/plugins/tend-ci-runner/skills/review/SKILL.md), "Draft mode") runs a lighter COMMENT-only review, never APPROVE (GitHub blocks approving drafts), and explicitly authorizes "Stay silent if there's nothing actionable; don't post a 'looks fine' comment." Before flagging a silent tend-review — or escalating to session logs to explain it — check `gh api repos/OWNER/REPO/pulls/N --jq '.draft'`: a silent first-review on a `draft: true` PR with clean findings is skill-designed silence, the sibling of the self-authored case above, not omission or divergence. This also covers a silent re-review after a human pushes to a still-draft PR.

- **`tend-mention` firing on the bot's own comments and exiting silently.** When the bot comments on an issue or PR where it has previously participated (including its own tracking issues such as `review-reviewers-tracking` and `review-runs-tracking`), the `issue_comment` event fires `tend-mention`; the prompt's self-conversation guard then detects the self-trigger and exits silently after a few Claude turns. The same shape occurs when the bot's own `tend-review` review mirrors back as a `pull_request_review` event and fires `tend-mention`. A deterministic verify-gate skip of the bot's *own comments* (`issue_comment` / `pull_request_review_comment` where `comment.user.login == bot`) is the accepted shape here and is implemented in the `verify` job — it suppresses no legitimate work, since the bot never needs to respond to its own comment. This is distinct from the authorship-keyed guards rejected for `tend-review`, where the bot's fix-push must still be re-reviewed (skipping it drops real work). The `pull_request_review` self-review path is *not* covered by that skip — it carries intentional actionable-signal handling (the empty-body-APPROVED gate) — so treat a lingering no-op on that path as expected, not a gap. For any new loop risk beyond the bot's own comments, prefer a label-based skip (e.g. skipping `tend-outage`-labeled issues); propose an authorship/sender filter only when the loop can't be expressed with a label *and* the skipped events carry no legitimate work.

- **`tend-triage` firing on the bot's own monthly tracking-issue *creation* and exiting via the self-conversation guard.** The `review-reviewers` / `review-runs` workflows auto-open a tracking issue on the 1st of each month; the `issues: opened` event triggers `tend-triage`, which reads the bot's own tracking issue and exits without posting. This is a designed no-op — the sibling of the `tend-mention`-on-own-comments case above, on the creation event rather than a comment. Do **not** propose a label-skip or filter to eliminate it: extending the triage `if:` to skip the tracking labels puts tend-internal label names in `.config/tend.yaml` plus a regenerated workflow to keep in sync, and that standing maintenance outweighs the few CI minutes a month the guard already absorbs cleanly. Record as carry observations and move on.

- **`tend-mention` firing a second time on `issue_comment.edited` after the commenter edits their own comment shortly after posting.** The `created` and `edited` events both trigger `tend-mention`; when the `created` run exits silently (no engagement) and the commenter then refines the comment within seconds, the `edited` retrigger runs the agent again and pays full context-load before silent-exiting via dedup-recheck. Do not propose a commenter-self-edit skip (e.g. `sender == comment.user.login` + short `updated_at - created_at` window): an edit can change relevance — adding substantive content that would now merit a response — and skipping the retrigger means the agent never re-evaluates the new body. The dedup-recheck handles "bot already responded"; it cannot handle "the edited content would now merit a response", and there is no recovery path once the retrigger is dropped. The recheck-before-posting silent-exit is the correct cost; record as carry observations and move on.

- **`tend-notifications` mark-reading a cross-repo `CheckSuite` `ci_activity` notification from an abandoned fork and exiting silently.** A fork that doesn't sync upstream keeps running its own scheduled `tend-*` workflows; when those fail, the canonical bot receives `reason: ci_activity` `CheckSuite` notifications about another repo's CI. The notifications skill classifies them as cross-repo not-actionable and marks them read, but the agent still spins up. The accepted shape is the workflow-level `github.repository_owner` fork-owner guard, which causes synced forks to no-op on the cron; the residual cost of abandoned-fork notifications is small enough not to justify a permanent pre-check layer. Do not propose a pre-check filter keyed on `reason`, `subject_type`, or sender, and do not maintain a per-month occurrence threshold for this notification shape — record as carry observations and move on.

## Target repo

**Target repo:** $ARGUMENTS

Analysis targets an adopter repo whose CI runs are analyzed. Findings result in PRs/issues on the current repo (tend) to improve skills and workflows.

Use `-R $ARGUMENTS` for commands that access the target repo (querying runs, PRs, issues). Commands without `-R` default to tend.

@review-gates.md

## Evidence accumulation

Each run only sees a window of CI sessions, but patterns emerge over days or weeks. Evidence for this skill lives in **secret gists owned by the bot** — one per `(target repo, month)` pair. A monthly tracking issue on tend labeled `review-reviewers-tracking` lists the gists via bot comments, so maintainers can discover them.

Secret gists are URL-unlisted but readable by anyone with the URL; they are at least as private as the current public tracking issues, and give a single structured file that accumulates per-target findings without hitting the 65 KB comment limit.

### Setup

```bash
MONTH=$(date +%Y-%m)
TRACKING_LABEL="review-reviewers-tracking"
TARGET="$ARGUMENTS"
GIST_DESC="review-reviewers evidence: $TARGET $MONTH"
```

### Finding or creating the tracking issue

The tracking issue lives on tend (the current repo). It indexes gists via one comment per new gist — no per-run comments, no body edits.

The workflow's `init-tracking` job runs before the matrix and creates the monthly tracking issue if absent, so on a normal tick matrix legs find an existing one. It is not a precondition — the matrix runs even when that job fails or never gets a runner, so the find-or-create logic below is a live code path on the first tick of a month, not only a fallback for ad-hoc invocations. Sort lowest-numbered first so a lost race degrades to a duplicate rather than a crash. `gh issue create` prints the new issue's URL; parse the number from its basename.

```bash
TRACKING_NUMBER=$(gh issue list --state open --label "$TRACKING_LABEL" \
  --json number,title --jq ".[] | select(.title | contains(\"$MONTH\")) | .number" \
  | sort -n | head -1)

if [ -z "$TRACKING_NUMBER" ]; then
  cat > /tmp/tracking-body.md << 'EOF'
Monthly tracking issue for `review-reviewers`. Per-target evidence lives in secret gists owned by the bot. A comment below is posted when each target's gist is first created.

**Do not close manually** — a new issue is created each month.
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

### Finding or creating the evidence gist

Search the bot's own gists by description. Descriptions are our stable key — GitHub does not let us pick gist IDs.

```bash
GIST_ID=$(gh api /gists --paginate \
  --jq ".[] | select(.description == \"$GIST_DESC\") | .id" | head -1)

if [ -z "$GIST_ID" ]; then
  # The gist file takes its name from the local file's basename; later reads
  # and PATCHes target `findings.md`, so the seed must live at that basename.
  mkdir -p /tmp/gist-seed
  # Use the Write tool to author /tmp/gist-seed/findings.md (substituting
  # $TARGET and $MONTH from the environment). Content:
  #
  #   # review-reviewers evidence — <target> — <YYYY-MM>
  #
  #   Secret gist. Append-only log of below-threshold findings used for gate evaluation.
  GIST_URL=$(gh gist create --desc "$GIST_DESC" /tmp/gist-seed/findings.md)
  if [ -z "$GIST_URL" ]; then
    echo "ERROR: gh gist create failed — TEND_BOT_TOKEN likely lacks 'gist' scope (see install-tend)" >&2
    exit 1
  fi
  GIST_ID=$(basename "$GIST_URL")
  # First time this month for this target — announce the gist on the tracking issue
  gh issue comment "$TRACKING_NUMBER" \
    --body "Evidence gist for \`$TARGET\`: $GIST_URL"
else
  GIST_URL="https://gist.github.com/$GIST_ID"
fi
```

The TEND_BOT_TOKEN needs `gist` scope (see install-tend). Without it, `gh gist create` fails with `403 Forbidden` and the skill exits before posting a broken tracking-issue comment.

### Reading historical evidence

Before applying the gates, read the current month's gist for this target. Pass `--raw` so `gh` emits the file content verbatim instead of a TTY-rendered form. The recording step below appends to this same file, so fetch once:

```bash
gh gist view "$GIST_ID" -f findings.md --raw > /tmp/current.md
```

Also check last month's gist for recent carry-over. Compute last month by subtracting a day from the first of the current month — `date -d 'last month'` on the 31st can return the current month on GNU date, silently skipping the prior month's evidence:

```bash
FIRST=$(date -u +%Y-%m-01)
LAST_MONTH=$(date -u -d "$FIRST -1 day" +%Y-%m 2>/dev/null || date -u -v-1d -jf %Y-%m-%d "$FIRST" +%Y-%m)
LAST_DESC="review-reviewers evidence: $TARGET $LAST_MONTH"
LAST_GIST_ID=$(gh api /gists --paginate \
  --jq ".[] | select(.description == \"$LAST_DESC\") | .id" | head -1)
[ -n "$LAST_GIST_ID" ] && gh gist view "$LAST_GIST_ID" -f findings.md --raw > /tmp/last-month-findings.md
```

### Recording below-threshold findings

**Append a `## Run <RUN_ID>` heading every run**, even when no problem finding exceeded a gate threshold. For all-clear hours, record a single Low-evidence "all-clear" entry as the body — runs analyzed, outcomes checked, no concerning signals. The heading per run is the audit trail that prior runs read to count cumulative occurrences and confirm which hours were analyzed; missing entries leave gaps that erode gate evaluation across runs.

After applying the gates, write each run's new findings (format in `@review-gates.md`) to `/tmp/findings.md`, then append them to the gist's `findings.md`. Reuse the current content already fetched into `/tmp/current.md` in "Reading historical evidence", concatenate, and PATCH via the API (`--rawfile` preserves trailing newlines that command substitution would strip):

```bash
# Verify the run heading references this run's $GITHUB_RUN_ID literally —
# fabricated round numbers produce dead Workflow links, see @review-gates.md.
grep -qF "$GITHUB_RUN_ID" /tmp/findings.md || {
  echo "ERROR: /tmp/findings.md does not contain \$GITHUB_RUN_ID=$GITHUB_RUN_ID — refusing to PATCH gist" >&2
  exit 1
}
cat /tmp/current.md /tmp/findings.md > /tmp/combined.md
jq -n --rawfile content /tmp/combined.md \
  '{files: {"findings.md": {content: $content}}}' \
  | gh api "/gists/$GIST_ID" -X PATCH --input -
```

Never replace wholesale — prior entries contain per-run evidence needed for gate evaluation. See `@review-gates.md` for the per-finding format.

## Step 1: Setup

Resolve the **target repo's** bot login and load repo-specific guidance upfront — both are needed throughout. `gh api user` returns the *analysis* bot (e.g., `tend-agent` when review-reviewers runs on tend), which is typically **not** the target repo's bot — filtering reviews/comments by the wrong login produces false "no bot output" negatives. Read `bot_name` from the target repo's `.config/tend.yaml`:

```bash
BOT_LOGIN=$(gh api "repos/$ARGUMENTS/contents/.config/tend.yaml" --jq '.content' 2>/dev/null \
  | base64 -d 2>/dev/null \
  | yq '.bot_name // ""' 2>/dev/null)
if [ -z "$BOT_LOGIN" ]; then
  echo "ERROR: could not resolve bot_name from $ARGUMENTS/.config/tend.yaml" >&2
  exit 1
fi
echo "BOT_LOGIN=$BOT_LOGIN (target: $ARGUMENTS)"
```

Read the target repo's repo-specific guidance to understand what the bot was told to do:

```bash
gh api "repos/$ARGUMENTS/contents/.claude/skills/running-tend/SKILL.md" \
  --jq '.content' | base64 -d
```

If the file doesn't exist, try common alternatives (`.claude/skills/running-tend.md`, `.claude/CLAUDE.md`). Understanding the repo's guidance is essential context for evaluating outcomes — without it, you'll misjudge authorized behavior as a violation.

Then list recently completed tend CI runs on the target repo:

```bash
TARGET_REPO=$ARGUMENTS ${CLAUDE_PLUGIN_ROOT}/scripts/list-recent-runs.sh
```

The script discovers `tend-*` workflows by default. Pass additional prefixes as arguments to include other workflows (e.g., `review-reviewers` when analyzing tend itself).

If empty, record the run as all-clear per "Recording below-threshold findings" above, then skip to Step 6.

## Step 2: Survey outcomes via cheap subagent

Spawn a cheap subagent to check outcomes across all runs from Step 1. The subagent does the token-heavy work of mapping runs to PRs/issues and checking acceptance signals.

Use a cheap subagent (e.g. Haiku / gpt-mini) and a prompt like:

> Survey bot outcomes on `$ARGUMENTS` for the following runs: [run IDs from Step 1].
> The bot's login is `$BOT_LOGIN`.
>
> For each run, determine:
> 1. Did the bot produce visible output (review, comment, issue action, commit)?
> 2. If yes, was the output accepted or rejected?
>
> **Sweep the window repo-wide before mapping any run, and report the row counts.** The per-run mapping below walks run → branch → PR → endpoint; a break anywhere in that chain returns empty for *every* run at once, and uniform silence reads as a quiet hour rather than as a broken query. These calls take no run ID, so they fail independently of it:
>
> ```bash
> WINDOW_START=<window start, ISO 8601>
> WINDOW_END=<window end, ISO 8601>
> IN_WINDOW="[.[] | select(.user.login == \"$BOT_LOGIN\")
>   | select((.created_at // .submitted_at) >= \"$WINDOW_START\"
>            and (.created_at // .submitted_at) <= \"$WINDOW_END\")] | length"
>
> gh api "repos/$ARGUMENTS/issues/comments?since=$WINDOW_START&per_page=100" --jq "$IN_WINDOW"
> gh api "repos/$ARGUMENTS/pulls/comments?since=$WINDOW_START&per_page=100" --jq "$IN_WINDOW"
>
> CANDIDATES=$(gh -R $ARGUMENTS pr list --state all --limit 100 \
>   --search "updated:>$WINDOW_START" --json number --jq '.[].number')
> echo "$CANDIDATES"
> for pr in $CANDIDATES; do
>   gh api "repos/$ARGUMENTS/pulls/$pr/reviews?per_page=100" --jq "$IN_WINDOW"
> done | jq -s add
> ```
>
> Filter the comment calls on `created_at` at both ends. `since` is an `updated_at` floor with no ceiling, so unfiltered it also returns comments written days earlier and merely edited in the window, plus everything posted between the window end and now — this skill starts 20–40 min after its tick, so on a busy repo that is most windows. Those are rows the per-run walk was right not to reach, and a check that contradicts a correct walk every run stops being believed. Leave `CANDIDATES` unbounded above: `updated:` matches the PR's own `updated_at`, so a range drops any PR that got bot output inside the window and was touched after it, and over-inclusion in a candidate list costs nothing. `--limit 100` is load-bearing — `gh pr list` defaults to 30 and truncates silently.
>
> The comment endpoints cover conversation and inline-review comments only; neither returns review submissions, and an empty-body `APPROVE` is `tend-review`'s most common output. Without the fourth count an approvals-only window reports `0, 0` and satisfies the gate below with two zeros carrying no signal. Report all four numbers at the top of your summary. If the sweep found in-window rows your per-run walk did not reach, the walk is wrong — re-map from the PR numbers the sweep found rather than reporting those runs as silent.
>
> **Acceptance needs a non-bot actor — name it.** For every acceptance or rejection signal, report the login that produced it: who merged, who reviewed, who replied, who pushed the follow-up. `$BOT_LOGIN` reviewing, replying to, or pushing to its own PR is the bot talking to itself, not acceptance. A non-bot merge *is* acceptance even when every commenter is the bot — the most common shape here is a maintainer merging a bot PR without ever posting. Reserve `bot-only — no human signal` for threads where `$BOT_LOGIN` is the only login across every surface. One `gh pr view` covers them all — merge actor, reviews, inline comments, conversation comments, commits:
>
> ```bash
> gh -R $ARGUMENTS pr view <pr> --json number,state,author,mergedBy,reviews,comments,commits --jq '{
>   pr: .number, state, author: .author.login, merged_by: .mergedBy.login,
>   reviews: [.reviews[] | {login: .author.login, state}],
>   logins: ([.mergedBy.login, .reviews[].author.login, .comments[].author.login,
>             .commits[].authors[].login] | map(select(. != null and . != "")) | unique)
> }'
> ```
>
> `logins` is the bot-only test: `["$BOT_LOGIN"]` or empty means no human touched the thread. Anything else, name that login and say which surface it came from. Every inline review comment belongs to a review record — including a standalone reply posted through the replies endpoint — so `reviews` covers inline commenters as well as submitted reviews, and its `state` gives the accept/reject direction. `comments` and `reviews` paginate in full. `commits` does not: `gh pr view` selects `commits(first: 100)` and never fetches a second page, oldest-first, so past 100 commits the newest drop out — exactly where a human follow-up push sits. On a PR that long, read the authors from `gh api "repos/$ARGUMENTS/pulls/<pr>/commits" --paginate` (itself capped at 250) before calling the thread bot-only.
>
> A comments-only check is not enough on its own: it misses both a silent maintainer merge (`mergedBy` set, nobody commenting) and a human `CHANGES_REQUESTED` that carries no inline comments.
>
> **How to map runs to outputs:**
> - `tend-review`: `gh -R $ARGUMENTS run view <run-id> --json headBranch` → find PR via
>   `gh -R $ARGUMENTS pr list --head <branch> --state all` → check bot reviews via
>   `gh api repos/$ARGUMENTS/pulls/<pr>/reviews`
> - `tend-notifications`: check for recent bot comments/issue-close events in the past hour
> - `tend-mention`: map run to issue/PR from triggering comment, check for bot replies
> - `tend-ci-fix`: map run → PR via `headBranch`, check for bot commits
>
> **Negative outcome signals** — report any sign the bot's output was rejected, corrected, or ignored. Common shapes (use judgment for signals not listed):
> - Human reviewer posted CHANGES_REQUESTED after bot approved
> - PR closed without merge shortly after bot approved
> - Bot posted no review despite a `tend-review` run completing on an open PR
> - Subsequent commits reversed changes the bot approved
> - Bot-closed issue was reopened
> - Fix commit was reverted or CI still failing after bot pushed
> - Human replied to bot with correction or complaint
> - Bot comment contains corruption (literal `${`, unescaped bangs, backslash-backticks, broken heredoc markers)
>
> **Corruption-scan recipe.** Save bot bodies to a file, then scan with `grep`:
>
> ```bash
> mkdir -p /tmp/bot-output && : > /tmp/bot-output/all.txt
> # Issue/PR comments (issue_comment endpoint)
> for n in <pr-or-issue-numbers>; do
>   gh api "repos/$ARGUMENTS/issues/$n/comments?per_page=100" \
>     --jq ".[] | select(.user.login == \"$BOT_LOGIN\" and .created_at > \"<window-start>\") | \"=== #$n issue-comment \(.id) ===\n\(.body)\n\"" \
>     >> /tmp/bot-output/all.txt
> done
> # Issue bodies (when bot opened the issue this window)
> for n in <bot-opened-issues>; do
>   gh api "repos/$ARGUMENTS/issues/$n" \
>     --jq "select(.user.login == \"$BOT_LOGIN\" and .created_at > \"<window-start>\") | \"=== ISSUE #$n body ===\n\(.body)\n\"" \
>     >> /tmp/bot-output/all.txt
> done
> # PR bodies (only when bot opened the PR this window)
> for n in <bot-opened-prs>; do
>   gh api "repos/$ARGUMENTS/pulls/$n" \
>     --jq "select(.user.login == \"$BOT_LOGIN\" and .created_at > \"<window-start>\") | \"=== PR #$n body ===\n\(.body)\n\"" \
>     >> /tmp/bot-output/all.txt
> done
> # PR reviews + inline review comments — any PR the bot reviewed/commented on, not just
> # bot-opened. tend-review's output ships on human-authored PRs (the most common surface)
> # which would never appear in <bot-opened-prs>.
> for n in <pr-numbers-bot-reviewed>; do
>   gh api "repos/$ARGUMENTS/pulls/$n/reviews" \
>     --jq ".[] | select(.user.login == \"$BOT_LOGIN\" and .submitted_at > \"<window-start>\") | \"=== PR #$n review \(.id) state=\(.state) ===\n\(.body)\n\"" \
>     >> /tmp/bot-output/all.txt
>   gh api "repos/$ARGUMENTS/pulls/$n/comments?per_page=100" \
>     --jq ".[] | select(.user.login == \"$BOT_LOGIN\" and .created_at > \"<window-start>\") | \"=== PR #$n inline-comment \(.id) ===\n\(.body)\n\"" \
>     >> /tmp/bot-output/all.txt
> done
> grep -nF '${' /tmp/bot-output/all.txt        # literal ${...} interpolation failure
> grep -nP '\\!' /tmp/bot-output/all.txt       # backslash-bang corruption
> grep -nP '\\`' /tmp/bot-output/all.txt       # backslash-backtick corruption
> grep -nE 'blob/main/.*#L[0-9]' /tmp/bot-output/all.txt  # un-pinned line links
> grep -nF 'anthropics/' /tmp/bot-output/all.txt         # wrong-owner URL
> ```
>
> Cover all four bot-output surfaces: issue comments, issue bodies, PR bodies, and reviews/inline review comments. Comments-only scans miss corruption that ships in a survey-issue or PR body.
>
> **Report format** — return a structured summary:
> ```
> ## Runs with no bot output (skipped)
> - <run-id>: <workflow> — <reason> (e.g., "no artifacts", "notification no-op")
>
> ## Runs with accepted output
> - <run-id>: <workflow> on PR #N — bot reviewed, PR merged by <login>
>
> ## Runs with bot-only threads (no human signal)
> - <run-id>: <workflow> on PR #N — participants: [<login>, ...]
>
> ## Runs with concerning output
> - <run-id>: <workflow> on PR #N — <signal> by <login> (e.g., "human posted CHANGES_REQUESTED")
>
> ## Sanity check
> <note if zero bot activity found across all runs — may indicate systemic failure>
> ```

A report of little or no bot output is only usable if it carries the repo-wide sweep counts — without them, run the sweep block yourself before believing it, because a silent window and a broken per-run walk produce the same summary. Absence is not a finding on its own either: don't reason from it toward a conclusion the sweep would contradict.

Review the subagent's summary, and verify any actor attribution before it enters a finding — a survey that credits the bot's own reply to a human turns a self-conversation into a false all-clear. Route on the buckets: if concerning outcomes exist, continue to Step 3. Otherwise judge the bot-only threads on their content — a self-review chain that went wrong is a finding even with no human in it, and one that read fine is not — and if nothing there concerns you and there are no sanity-check flags, skip to Step 6 (summary).

## Step 3: Investigate concerning outcomes via cheap subagent

For runs with negative outcome signals (or suspicious lack of output), spawn another cheap subagent to download and inspect the specific session logs.

Use a cheap subagent (e.g. Haiku / gpt-mini) and a prompt like:

> Investigate session logs for run <run-id> on `$ARGUMENTS`.
>
> Download: `gh run download <run-id> -R $ARGUMENTS --pattern 'claude-session-logs*' --pattern 'codex-session-logs*' --dir /tmp/session-logs/<run-id>/` (both patterns are passed because the artifact prefix depends on the target repo's harness — Claude uploads `claude-session-logs*`, Codex uploads `codex-session-logs*`)
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

Evaluate the subagent's diagnosis against the repo-specific guidance from Step 1. Determine whether the failure is structural (same conditions always produce this failure) or stochastic (probabilistic model behavior that might not recur).

## Step 4: Deduplicate

Before creating issues or PRs, check exhaustively for existing ones:

```bash
gh issue list --state open --label claude-behavior --json number,title,body
gh issue list --state open --json number,title,body  # also check unlabeled issues
gh issue list --state closed --label claude-behavior --json number,title,closedAt --limit 30
# --state all: a merged PR is the most common way a finding is already fixed
gh pr list --state all --limit 40 --json number,title,state,mergedAt,headRefName,body
```

**A merged fix still reproduces on adopters.** Adopters call a pinned action ref, so a merged skill fix is dormant on their repos until the next release tags. Observing the bug is therefore not evidence the fix is missing — check merged PRs before filing, or the report is churn on something already landed.

Search titles AND bodies for related keywords. Only comment on existing issues if you have material new cases that would change the approach or increase prioritization. Do not comment with progress updates, fix-PR status, or re-statements of evidence already in the issue.

## Step 5: Act on findings

**Prefer PRs over issues.** A PR with a clear description is immediately actionable.

- **PR** (default): Branch `hourly/review-$GITHUB_RUN_ID-<target-repo-name>-<topic-slug>`, fix, commit, push, create with label `claude-behavior`. `$GITHUB_RUN_ID` alone is not a unique branch name: every matrix leg of a tick carries the same one, and a single leg may open two PRs (see the 2-PR limit below). The target's repo name (the part after the `/`) keeps two legs from racing the same ref; the topic slug keeps one leg's two PRs from doing the same. Put full analysis in PR description (run ID, outcome evidence, root cause, **gate assessment** including historical evidence count). Don't also create a separate issue.
- **Issue** (fallback): Only for problems too large or ambiguous to fix directly. Include run ID, outcome evidence, root cause analysis.

Group multiple findings by broad theme. **Limit to at most 2 PRs per run** — if you have more findings, pick the highest-confidence ones and record the rest in the evidence gist.

PR/issue bodies should link to the evidence gist (`$GIST_URL`) so reviewers can see the accumulated history behind the finding.

**Do not poll CI** after creating a PR. The `tend-review` and `tend-ci-fix` workflows handle PRs independently. Exit after pushing and creating the PR.

## Step 6: Summary

Report results in the conversation log and save a markdown summary to `/tmp/claude/step-summary.md` (a post-Claude step copies this into the GitHub Actions step summary). Use the Write tool. Include `$GIST_URL` at the top so maintainers viewing the run page can click through to the full evidence log:

```bash
mkdir -p /tmp/claude
# Then use the Write tool to author /tmp/claude/step-summary.md, starting:
#
#   ## Review-reviewers summary
#
#   Evidence: <value of $GIST_URL>
#
#   ...
```

If no problems found (or none passed the gates), report "all clear" with: runs analyzed, outcomes checked, brief quality assessment, and a link to the evidence gist for any below-threshold findings recorded this run.
