---
name: review-reviewers
description: Outcome-based analysis of tend's CI behavior — checks whether tend's outputs were accepted or rejected, escalating to session logs only when outcomes look wrong.
argument-hint: "<owner/repo>"
metadata:
  internal: true
---

# Review Reviewers

Analyze tend's CI behavior on the target repo over the window Step 1 returns. Focus on **outcomes** — what the bot produced publicly and whether it was accepted — rather than internal session mechanics. Create PRs or issues on tend when outcomes reveal behavioral problems.

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

Some patterns look suspicious but are intentional — flagging expected behavior creates maintainer churn and costs trust. Three structural rules cover them:

- **Designed no-ops.** Many events correctly end with nothing posted, at whatever layer catches them: a pre-boot gate skip (`tend-mention`'s verify gate on the bot's own comments and no-op self-reviews — though targets on older pinned releases still boot sessions for those), or a session that boots and exits silently (`tend-triage` on the bot's own monthly tracking-issue creation; the `issue_comment.edited` retrigger after a commenter refines their comment — the edit can change relevance, so the retrigger must re-evaluate; `tend-notifications` mark-reading a cross-repo `ci_activity` notification from an abandoned fork). These cost compute, not correctness — Gate 3 classifies them waste-class: record and move on; do not propose a skip-gate, label filter, pre-check, or occurrence threshold to save the boot. A loop that produces *wrong outward actions* (duplicate comments, spurious reviews) is different — that passes Gate 3, and a label-based skip is preferred over an authorship filter where a label can express it.

- **Designed silence.** The bundled `review` skill authorizes posting nothing when there is nothing actionable: on a self-authored PR (GitHub rejects self-approvals, so APPROVE isn't an option), on a draft PR (COMMENT-only mode; GitHub blocks approving drafts), or when the PR closed or merged while its run was queued. GitHub reports drafts as `state: OPEN`, so before reading a missing review as omission — or escalating to session logs to explain it — check `gh api repos/OWNER/REPO/pulls/N --jq '{state, draft}'` and the PR's literal author (`gh pr view <n> --json author --jq '.author.login'`; owner-authored PRs are approved normally and are no bot-authored-APPROVE precedent).

- **The reviewer role is independent of authorship.** `tend-review` re-reviewing — and re-approving — after any tend workflow pushes a fix commit is the design, not a re-approval loop; authorship-keyed guards that skip re-review drop real work and are not an accepted shape. Stacked approvals from racing runs are a *concurrency* artifact (cancelled runs POSTing before the SIGTERM arrived), not a review-rule problem.

## Target repo

**Target repo:** $ARGUMENTS

Analysis targets an adopter repo whose CI runs are analyzed. Findings result in PRs/issues on the current repo (tend) to improve skills and workflows.

Use `-R $ARGUMENTS` for commands that access the target repo (querying runs, PRs, issues). Commands without `-R` default to tend.

@review-gates.md

## Evidence accumulation

Evidence lives in one secret gist per target repo and month, indexed by the
monthly `review-reviewers-tracking` issue on Tend. Prepare it and read the
current and previous month's evidence:

```bash
uv run --script \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_reviewers.py" \
  prepare-evidence "$ARGUMENTS"
```

The command finds or creates both index and gist, announces a new gist once,
persists their ids, and prints both evidence windows.

After applying the gates, write this run's findings in the format from
`@review-gates.md` to `/tmp/findings.md`. Append a `## Run
$GITHUB_RUN_ID` heading every run, including an all-clear window; the heading
is the audit trail future runs use. Then append it:

```bash
uv run --script \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_reviewers.py" append-evidence
```

The command refuses a findings file that does not name this run, fetches the
latest gist content, and appends without replacing prior evidence.

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
TARGET_REPO=$ARGUMENTS uv run --script \
  "${CLAUDE_PLUGIN_ROOT}/scripts/list_recent_runs.py" review-reviewers
```

The script discovers `tend-*` workflows by default. Pass additional prefixes as arguments to include other workflows (e.g., `review-reviewers` when analyzing tend itself).

If empty, record the run as all-clear per "Recording below-threshold findings" above, then skip to Step 6.

If the script printed a `WARNING:` on stderr, the list is known-incomplete — the window was clamped, no anchor was found, or a workflow hit the fetch limit. Record a coverage gap naming the missing span instead of an all-clear, whether or not the list came back empty; the next run's floor advances past that span regardless, so an unrecorded gap is never revisited. If the script *fails* (non-zero exit, e.g. a transient API error), re-run it once; if it fails again, record the window as a coverage gap the same way — this run still concludes green, so the next tick anchors on it and never revisits the span.

**State the window you analyzed.** Its floor is the previous successful run of this workflow, or 6h back when that is older. This workflow is dispatch-only, so runs sit further apart than a cron's and the floor moves accordingly: scope every claim to it — "no problems since 08:12Z", never "no problems" — and say plainly when the run was dispatched to check on something that landed before it.

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
> Filter the comment calls on `created_at` at both ends. `since` is an `updated_at` floor with no ceiling, so unfiltered it also returns comments written days earlier and merely edited in the window, plus everything posted between the window end and now — the session's own runtime keeps that gap open, so on a busy repo it catches rows in most windows. Those are rows the per-run walk was right not to reach, and a check that contradicts a correct walk every run stops being believed. Leave `CANDIDATES` unbounded above: `updated:` matches the PR's own `updated_at`, so a range drops any PR that got bot output inside the window and was touched after it, and over-inclusion in a candidate list costs nothing. `--limit 100` is load-bearing — `gh pr list` defaults to 30 and truncates silently.
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
> - `tend-notifications`: check for bot comments/issue-close events inside the window from Step 1
> - `tend-mention`: map run to issue/PR from triggering comment, check for bot replies
> - `tend-mention` on `repository_dispatch` (the relay path for review events): there is no triggering comment and `headBranch` is the default branch, so neither route above resolves it. Read the target off the `verify` job's log, where the step env block prints the relayed payload:
>   ```bash
>   JOB=$(gh api "repos/$ARGUMENTS/actions/runs/<run-id>/jobs" --jq '.jobs[] | select(.name == "verify") | .id')
>   gh api "repos/$ARGUMENTS/actions/jobs/$JOB/logs" | grep -E 'PAYLOAD_(KIND|PR|ID):'
>   ```
>   `PAYLOAD_PR` is the issue/PR number and `PAYLOAD_KIND` is the relayed event (`pull_request_review`, `pull_request_review_comment`). Read the gate's verdict off the `handle` job, which is gated on `should_run`: `handle` with conclusion `skipped` means the engagement gate declined and no agent booted — expected silence, not missing output. Do not read it off `React to mention`, which is skipped on every relayed `pull_request_review` regardless of the verdict (a review submission has no single comment to react to) and on a comment relay admitted for participation rather than a mention.
> - `tend-ci-fix`: map run → PR via `headBranch`, check for bot commits
>
> **Negative outcome signals** — report any sign the bot's output was rejected, corrected, or ignored. Common shapes (use judgment for signals not listed):
> - Human reviewer posted CHANGES_REQUESTED after bot approved
> - PR closed without merge shortly after bot approved
> - Bot posted no review despite a `tend-review` run completing on an open PR — first check whether designed silence applies (draft or self-authored PR), then inspect the session logs if it does not.
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

**Also escalate whenever a finding will name *which run* produced an output.** A timestamp falling inside a run's start/end span does not attribute the output to it: several runs are live at once — a long scheduled run that opened the PR, the event-triggered handle answering a review on it, a racing sibling — and any of them can post. Attributing by wall-clock inclusion credits the wrong run, and the run ID then ships in a public comment. Confirm from the posting run's own log before a run ID enters a finding.

Grep for the write *shape*, not the endpoint — `/comments` and `/reviews` are the GET paths too, and reading the thread is the first thing nearly every run does. Don't truncate the tool input: a `git push` or `gh issue comment` often sits hundreds of characters into a longer command. Scan every `*.jsonl` under the run directory, since a subagent posts from its own `subagents/agent-*.jsonl`.

```bash
WRITES='gh (pr|issue) (comment|review|create|edit)|--method (POST|PATCH|PUT)|-X (POST|PATCH|PUT)|comments/[0-9]+/replies|git push|resolveReviewThread'

# Claude logs
for f in $(find /tmp/session-logs/<run-id> -name '*.jsonl'); do
  jq -r 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use") | "\(.name): \(.input | tostring)"' "$f"
done | grep -E "$WRITES"

# Codex logs — same idea against the rollout schema (/install-tend:debug-tend-run)
for f in $(find /tmp/session-logs/<run-id> -name '*.jsonl'); do
  jq -r 'select(.payload.type == "function_call") | "\(.payload.name): \(.payload.arguments)"' "$f"
done | grep -E "$WRITES"
```

The run whose log contains the posting call is the author. Presence is the strong signal; before concluding a run posted *nothing*, confirm you ran the variant matching the artifact you downloaded — the wrong one returns empty regardless of what the run did — and that the write you're chasing has a shape `WRITES` covers.

Use a cheap subagent (e.g. Haiku / gpt-mini) and a prompt like:

> Investigate session logs for run <run-id> on `$ARGUMENTS`.
>
> Download: `gh run download <run-id> -R $ARGUMENTS --pattern 'claude-session-logs*' --pattern 'codex-session-logs*' --dir /tmp/session-logs/<run-id>/` (both patterns are passed because the artifact prefix depends on the target repo's harness — Claude uploads `claude-session-logs*`, Codex uploads `codex-session-logs*`)
>
> The concerning outcome was: <signal from Step 2>.
>
> If your answer will name **which run produced an output**, say which output and confirm it from the log before answering — run <paste the `WRITES` recipe above> over every `*.jsonl` under the download directory, and quote the matching tool call verbatim rather than summarising it. Don't use the truncated query below for this: the write often sits hundreds of characters into a longer command.
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
gh issue list --state open --label claude-behavior --limit 200 --json number,title,body
gh issue list --state open --limit 200 --json number,title,body  # also check unlabeled issues
gh issue list --state closed --label claude-behavior --json number,title,closedAt --limit 30
# --state all: a merged PR is the most common way a finding is already fixed
gh pr list --state all --limit 40 --json number,title,state,mergedAt,headRefName,body
```

**A merged fix still reproduces on adopters.** Adopters call a pinned action ref, so a merged skill fix is dormant on their repos until the next release tags. Observing the bug is therefore not evidence the fix is missing — check merged PRs before filing, or the report is churn on something already landed.

Search titles AND bodies for related keywords. Only comment on existing issues if you have material new cases that would change the approach or increase prioritization. Do not comment with progress updates, fix-PR status, or re-statements of evidence already in the issue.

## Step 5: Act on findings

**Prefer PRs over issues.** A PR with a clear description is immediately actionable.

- **PR** (default): Branch `review-reviewers/$GITHUB_RUN_ID-<target-repo-name>-<topic-slug>`, fix, commit, push, create with label `claude-behavior`. `$GITHUB_RUN_ID` alone is not a unique branch name: every matrix leg of a run carries the same one, and a single leg may open two PRs (see the 2-PR limit below). The target's repo name (the part after the `/`) keeps two legs from racing the same ref; the topic slug keeps one leg's two PRs from doing the same. Write the description for a maintainer deciding whether the current change fixes the general behavior gap, following **Reader-facing prose** in `running-in-ci`. The evidence gist already carries the run history, outcome evidence, and gate assessment; link it and include only what the reader needs to understand this change. Don't also create a separate issue.
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
