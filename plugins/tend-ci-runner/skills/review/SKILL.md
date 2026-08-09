---
name: review
description: Reviews a pull request for code quality and correctness. Use when asked to review a PR or when running as an automated PR reviewer.
argument-hint: "[PR number]"
metadata:
  internal: true
---

# PR Review

Review a pull request.

**PR to review:** $ARGUMENTS

## Workflow

Follow these steps in order.

### 0. Load environment skills

Load `/tend-ci-runner:running-in-ci` first — it contains CI security rules, polling conventions, and comment formatting guidance. It will also prompt you to load any repo-specific skills (e.g., `running-tend`).

### 1. Pre-flight checks

Before reading the diff, run cheap checks to avoid redundant work. Shell state doesn't persist between tool calls — re-derive `REPO` in each bash invocation or combine commands.

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
BOT_LOGIN=$(gh api user --jq '.login')
HEAD_SHA=$(gh pr view <number> --json commits --jq '.commits[-1].oid')
PR_AUTHOR=$(gh pr view <number> --json author --jq '.author.login')
IS_DRAFT=$(gh pr view <number> --json isDraft --jq '.isDraft')
EVENT_ACTION=$(jq -r '.action // ""' < "${GITHUB_EVENT_PATH:-/dev/null}" 2>/dev/null)

# Find the bot's most recent *real* review. Replying to a review thread
# (`POST /pulls/{n}/comments/{id}/replies`) makes GitHub wrap the reply in a
# synthetic zero-body COMMENTED review anchored at the then-current HEAD, so the
# newest review record routinely points at a commit nothing reviewed. Count a
# record only when it has a body, owns a top-level (non-reply) inline comment, or
# is an APPROVED review — reply containers are always zero-body COMMENTED, while
# an empty-body approval and an empty-body review carrying real inline findings
# both still anchor.
SUBSTANTIVE=$(gh api --paginate "repos/$REPO/pulls/<number>/comments" \
  --jq '.[] | select(.in_reply_to_id == null) | .pull_request_review_id' | jq -s 'unique')

# IMPORTANT: REST reviews carry `.commit_id`, NOT the `.commit.oid` that
# `gh pr view --json reviews` returns — don't confuse the two. `gh api --jq`
# accepts no `--arg`/`--argjson`, so pipe to `jq` rather than using `--jq`.
LAST_REVIEW=$(gh api --paginate "repos/$REPO/pulls/<number>/reviews" \
  | jq -s --argjson sub "$SUBSTANTIVE" --arg bot "$BOT_LOGIN" \
    'add | [.[] | select(.user.login == $bot)
                | select((.body | length) > 0 or (.id | IN($sub[])) or .state == "APPROVED")]
         | last // {}')
LAST_REVIEW_SHA=$(jq -r '.commit_id // empty' <<<"$LAST_REVIEW")

# A force-push does not just move HEAD — GitHub re-points the prior review's
# `.commit_id` at the NEW head, so `LAST_REVIEW_SHA == HEAD_SHA` reads true for
# code nothing ever reviewed and the run exits silently, leaving a stale APPROVE
# standing as the active verdict. (An ordinary push leaves the old anchor
# intact; only a rewrite re-points it, so `.commit_id` alone can't tell the two
# apart.) Probe the timeline for a rewrite newer than that review.
LAST_REVIEW_AT=$(jq -r '.submitted_at // empty' <<<"$LAST_REVIEW")
FORCE_PUSHED=0
if [ -n "$LAST_REVIEW_AT" ]; then
  FORCE_PUSHED=$(gh api --paginate "repos/$REPO/issues/<number>/timeline" \
    | jq -s --arg at "$LAST_REVIEW_AT" \
      'add | [.[] | select(.event == "head_ref_force_pushed" and .created_at > $at)] | length')
fi
```

If `FORCE_PUSHED` is non-zero, the commit the bot reviewed was rewritten away: ignore `LAST_REVIEW_SHA` entirely and review `HEAD_SHA` in full. The incremental below can't run either — `LAST_REVIEW_SHA` now names the current head rather than anything the bot read, so `LAST_REVIEW_SHA..HEAD_SHA` is empty and every trivial-skip heuristic keyed on it under-reports. If that prior review was an `APPROVED` and the re-review lands on findings rather than an approval, dismiss it too — it is re-anchored onto the rewritten head, so posting a COMMENT alone leaves the PR reading as bot-approved. `jq -r '.state, .id' <<<"$LAST_REVIEW"` gives the state and the `$REVIEW_ID` for step 6's `reviews/$REVIEW_ID/dismissals` call.

Otherwise, if `LAST_REVIEW_SHA == HEAD_SHA`, this commit has already been reviewed — finish at step 9 without posting. Two exceptions: an unanswered conversation question directed at the bot (check below), or `EVENT_ACTION == "ready_for_review"` (the PR just transitioned out of draft, so any prior review was a draft-mode review and the author is now asking for a full one — proceed).

If the bot reviewed a previous commit (`LAST_REVIEW_SHA` exists but differs from `HEAD_SHA`), judge what was pushed since. Read two signals, both leak-free against base-merges:

- The PR's three-dot diff — `gh pr diff <number>` (merge-base→head, the same diff step 3 uses) — for the current state. Base-merge commits never enter it.
- The **accurate incremental** — commits authored since the last review, with per-file line counts — for the trivial-skip decision below.

```bash
# Commits + per-file line counts authored since the last review.
#
# --no-merges alone is NOT enough: it drops the merge *commit*, but a base
# merge also pulls in the base branch's own non-merge commits, which are
# reachable from $HEAD_SHA but not from $LAST_REVIEW_SHA — so they sit in the
# A..B range and their churn gets summed as if it were the new push. Exclude
# everything reachable from the base tip with `--not "$BASE_SHA"`: base churn
# is always an ancestor of the base tip, while the PR's own commits are not,
# so this isolates exactly the authored incremental (overlapping files
# included). The review checkout is fetch-depth: 0; fetch the base tip in case
# the /head conflict-fallback checkout didn't materialize it.
BASE_SHA=$(gh pr view <number> --json baseRefOid --jq '.baseRefOid')
git fetch --no-tags --quiet origin "$BASE_SHA" 2>/dev/null || true
git log --no-merges --numstat --format='%h %s' "$LAST_REVIEW_SHA..$HEAD_SHA" --not "$BASE_SHA"
```

If the incremental changes are trivial, skip the full review — go directly to step 7 to resolve any bot threads addressed by the new changes. After resolving threads: if the most recent bot review was a COMMENT that flagged issues, and those issues are now addressed, submit an APPROVE with an empty body so the PR isn't left in limbo. Otherwise do not submit a new review — the existing one stands. Do NOT proceed to steps 2–6; finish at step 9. Rough heuristic: changes under ~20 added+deleted lines that don't introduce new functions, types, or control flow are typically trivial.

**Commit and PR authorship do not affect review behavior.** Apply the same trivial-vs-substantive heuristic regardless of who pushed the new commits. When `tend-notifications` or `tend-ci-fix` pushes a fix to a human-authored PR, reviewing (and re-approving) the updated state is expected — the reviewer role is independent of commit authorship.

Then read all previous bot feedback and conversation:

```bash
BOT_LOGIN=$(gh api user --jq '.login')
# Conversation comments + previous review bodies (one fetch)
gh pr view <number> --json comments,reviews \
  --jq "{prev_reviews:  [.reviews[]  | select(.author.login == \"$BOT_LOGIN\"
                                              and (.body | length > 0)) | {state, body}],
         conversation:  [.comments[] | {author: .author.login, body}]}"

# Inline review comments — separate path (gh pr view --json doesn't include them)
cat > /tmp/inline-prev.graphql <<'GRAPHQL'
query($owner:String!,$repo:String!,$number:Int!) {
  repository(owner:$owner,name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        nodes { comments(first:100) { nodes {
          author { login } path line body
        } } }
      }
    }
  }
}
GRAPHQL
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
OWNER=${REPO%/*}; NAME=${REPO#*/}
gh api graphql -F query=@/tmp/inline-prev.graphql -f owner="$OWNER" -f repo="$NAME" -F number=<number> \
  --jq ".data.repository.pullRequest.reviewThreads.nodes[].comments.nodes[]
        | select(.author.login == \"$BOT_LOGIN\")
        | {path, line, body}"
```

**Apply the sibling-workflow dedup rule from `running-in-ci`** to both the review body and inline comments. If a prior bot comment in the conversation already covers a point — a previous review on this or an earlier commit, a `tend-mention` reply, a `tend-triage` post, anything from a tend workflow — omit it from this review and stick to diff-grounded findings. If that leaves no new diff-grounded finding on the incremental changes and the only outstanding concern is a still-unresolved thread from an earlier bot review, do not post a new review: that thread already blocks the PR, and restating "the prior thread still applies" on every push is noise. Resolve any bot threads the new commits addressed (step 7), then finish at step 9 without posting. A fresh review is warranted only when the incremental diff introduces a new finding, or resolves the last open one (then approve with an empty body). When concurrent runs race (a new push while the first run is still responding), both see the same unanswered question — check whether a bot reply exists after the question's timestamp before answering. Address remaining unanswered questions in the review body (not via `gh pr comment`).

#### Draft mode

If `IS_DRAFT == "true"`, run a lighter review:

- Skip step 2 (overlap with other PRs) — landing-readiness concern, premature for WIP.
- Skip the duplication scan in step 4 — the author is still shaping the design.
- Submit as **COMMENT only**, never APPROVE. GitHub blocks approving drafts, and the author hasn't asked for a verdict yet.
- Open the review body with one line framing it: `Reviewing as a draft — flagging anything that looks worth a quick fix. Mark ready for a full review.`
- Skip step 6 (CI monitoring) — drafts churn; CI failures are the author's to chase.
- Skip step 8 (push fixes) — never push to a WIP branch.

Steps 1, 3, 4 (without duplication scan), 5 (COMMENT path), 7, and 9 still apply. Stay silent if there's nothing actionable; don't post a "looks fine" comment.

### 2. Check for overlapping PRs

Before reading the diff, scan other open PRs for file overlap. If another PR touches the same files with a similar fix, flag it in the review so one can be closed as a duplicate.

### 3. Read and understand the change

1. Read the PR diff with `gh pr diff <number>`.
2. Before going deeper, look at the PR as a reader would — not just the code, but the shape: what files are being added/changed, and does anything look off?
3. Read the changed files in full (not just the diff) to understand context.

### 4. Review

Scale depth to the change. A docs-only PR or a mechanical rename needs a skim for correctness, not the full checklist. A new algorithm or state-management change needs trace analysis. Don't over-analyze trivial changes.

Check the project's CLAUDE.md for language-specific review criteria and conventions. Load any project-specific review skill if available.

Review the diff two ways at once: the manual checks below, plus a `/tend-ci-runner:code-review` pass over the PR's merged tree. That skill is a structured second pass — correctness and cleanup angles, then a verify pass — and it returns findings rather than posting anything. Scale its depth to how core the change is, the same way you scale the manual depth above:

- Peripheral or mechanical (docs, config, dependency bumps, test-only): tell it the change is peripheral, so it runs the short angle set in one pass.
- The project's core logic: tell it the change is core, so it fans the angles out and sweeps for gaps.

What counts as core is repo-specific; let the project's own guidance (CLAUDE.md, a repo review skill) or your judgment decide. Both passes feed one verdict: fold its findings into the review you submit in step 5. It only reports back — it never posts a review, comment, or commit of its own, so the dedup and single-review path is preserved.

**Code quality:**

- Is the code clear and well-structured?
- Are there simpler ways to express the same logic?
- Does it avoid unnecessary complexity, feature flags, or compatibility layers?

**Correctness:**

- Are there edge cases that aren't handled?
- Could the changes break existing functionality?
- Are error messages helpful and consistent with the project style?
- **Trace failure paths, don't just note error handling exists.** For code that modifies state through multiple fallible steps, walk through what happens when each error fires. What has already been mutated? Is the system left in a recoverable state?

**Testing:**

- Are the changes adequately tested?

**Same pattern elsewhere:**

When a PR fixes a bug or changes a pattern, search for the same pattern in other files. If found in the diff, add inline suggestions; if found outside the diff, offer to push a fix commit.

**Citing code outside the diff:**

The checkout uses `refs/pull/N/merge`, so you read the merged tree (PR head + current base branch). When a finding involves code outside `gh pr diff` — code the PR didn't touch but now interacts with — identify the interacting code by semantic anchor (selector, function name, declaration text) and quote enough of the relevant property/value that the reader can grep for it. Don't lead with the line number: the author's local branch may be behind main, so line numbers from the merged tree won't match their checkout, and a line-number-first citation reads as a hallucination when they check the line on their stale branch. Same principle as the `blob/main/...#L42` link rule in `running-in-ci` — line numbers are fragile across diverging refs.

**Duplication check (for new functions/types):**

For every new public or module-level function added in the diff, search the codebase for existing functions that do the same thing. LLM-generated code frequently reinvents internal APIs — this is the highest-value check for externally contributed PRs.

Two search strategies, both required:

1. **Similar names and signatures.** Search for functions with similar names, return types, or parameter types.
2. **Overlapping subgoals.** Identify the intermediate steps the new code performs and search for existing code that does the same sub-tasks.

Flag duplicates — reuse is almost always better than a parallel implementation.

### 5. Submit

**If there are no issues, approve with an empty body — silence means correct.**

```bash
gh pr review <number> --approve -b "" && echo "✓ approved"
```

If there are actionable findings, submit as a review with inline suggestions for concrete fixes. Every comment must give the author something to act on:

| Don't post (internal analysis) | Post (actionable) |
|---|---|
| "The fix correctly delegates to X" | "The error message still references the old behavior" |
| "The threshold logic is correct" | _(nothing — silence means correct)_ |

Don't explain what the code does — the author wrote it. Don't nitpick formatting — that's what linters are for. Explain *why* something should change, not just *what*.

**Form your own opinion independently.** Do not factor in other reviewers' comments or approvals when deciding whether to approve — the value of this review is as an uncorrelated signal.

**When confidence is low**, go beyond checking the implementation — question the approach: "Does this bypass or duplicate an existing API?" "What does this change *not* handle?" If the design involves a judgment call, flag it for human review as a COMMENT.

**Self-authored PRs** (`PR_AUTHOR == BOT_LOGIN` — compare the literal bot login string, not "authored by someone senior" or "by the repo owner"): Still perform the full review (steps 2-3) — self-review catches real issues (lint failures, edge cases) and is intentionally valuable. Do NOT attempt `gh pr review --approve` — GitHub rejects self-approvals. Submit as COMMENT when there are concerns, or stay silent and skip to step 6. Always post CI failure analysis as a COMMENT, even on self-authored PRs.

**Not confident enough to approve** (unfamiliar module, subtle logic): Add a `+1` reaction instead — no review needed unless there are specific observations.

```bash
gh api "repos/$REPO/issues/<number>/reactions" -f content="+1"
```

#### Posting mechanics

Before posting, verify HEAD hasn't moved, the PR is still open, and no review was already posted for this commit:

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
BOT_LOGIN=$(gh api user --jq '.login')
read -r CURRENT_HEAD PR_STATE < <(gh pr view <number> --json commits,state \
  --jq '"\(.commits[-1].oid) \(.state)"')
[ "$CURRENT_HEAD" != "$HEAD_SHA" ] && echo "HEAD moved — fold in per step 9, then re-run these mechanics against $CURRENT_HEAD" && exit 0
[ "$PR_STATE" != "OPEN" ] && echo "PR is $PR_STATE — skipping" && exit 0

# Same substantive-review filter as step 1, and for the same reason: a synthetic
# reply container is anchored at whatever HEAD was when the reply posted, so one
# left on the current HEAD would read as "already reviewed" and discard this run's
# review at the last step, after all the work. Shell state doesn't carry between
# tool calls, so re-derive $SUBSTANTIVE here.
#
# The force-push re-anchoring from step 1 lands on this guard too, and harder: a
# review submitted against the rewritten-away commit now reports
# `.commit_id == $HEAD_SHA`, so it reads as "already reviewed" and discards the
# very re-review step 1's `FORCE_PUSHED` probe just unblocked. Drop reviews older
# than the newest rewrite; anything submitted after it really did anchor here.
# NOTE: REST API uses .commit_id (not .commit.oid from gh pr view --json)
SUBSTANTIVE=$(gh api --paginate "repos/$REPO/pulls/<number>/comments" \
  --jq '.[] | select(.in_reply_to_id == null) | .pull_request_review_id' | jq -s 'unique')
LAST_FORCE_PUSH_AT=$(gh api --paginate "repos/$REPO/issues/<number>/timeline" \
  | jq -rs 'add | [.[] | select(.event == "head_ref_force_pushed") | .created_at] | max // ""')
ALREADY_POSTED=$(gh api --paginate "repos/$REPO/pulls/<number>/reviews" \
  | jq -rs --argjson sub "$SUBSTANTIVE" --arg bot "$BOT_LOGIN" --arg head "$HEAD_SHA" \
    --arg fp "$LAST_FORCE_PUSH_AT" \
    'add | [.[] | select(.user.login == $bot and .commit_id == $head)
                | select($fp == "" or .submitted_at > $fp)
                | select((.body | length) > 0 or (.id | IN($sub[])) or .state == "APPROVED")]
         | last | .submitted_at // empty')
[ -n "$ALREADY_POSTED" ] && echo "Already reviewed — finish at step 9" && exit 0
```

The state check matters because the maintainer may close (or another path may merge) the PR while a review is in flight — `pull_request_target` reviews routinely run for 5–10 minutes between fetching `HEAD_SHA` and posting the verdict, and HEAD doesn't move when the PR is closed. Approving a CLOSED or MERGED PR creates a confusing artifact (an approval timestamped after the close).

When HEAD moved, don't discard the assembled review — no other run will pick it up: the concurrency group holds the queued replacement until this session ends, and the pre-check skips it once step 9 stamps what this session examined. Instead judge the new commits with step 1's incremental logic, drop findings they addressed, fold in any new ones, and post one review against `$CURRENT_HEAD` (re-run these mechanics against it first).

**Before APPROVE specifically**, also peek the current check rollup on `HEAD_SHA`. If any check has reached terminal `FAILURE`, do not emit an empty-body APPROVE — the close-out reads as the bot rubber-stamping over the visibly red signal.

Reduce the rollup to the **latest entry per check name and workflow** before reading it. When a concurrency-cancelled run is replaced, GitHub keeps *both* check runs on the commit, so an un-deduped scan reports the superseded `FAILURE` alongside the replacement's `SUCCESS` — and keeps reporting it forever. Key on `workflowName` as well as the name: two workflows can register the same check name, and collapsing those into one entry would hide a genuine red behind an unrelated green.

```bash
ROLLUP=$(gh pr view <number> --json statusCheckRollup \
  --jq '[.statusCheckRollup[]] | group_by([.name // .context, .workflowName]) | map(max_by(.startedAt))')

FAILED=$(jq -r '[.[]
         | select((.conclusion // .state) == "FAILURE")
         | .name // .context // "unknown"] | join(", ")' <<<"$ROLLUP")
PENDING=$(jq --arg own "/runs/$GITHUB_RUN_ID/" --arg wf "$GITHUB_WORKFLOW" '
      [.[]
       | select((.detailsUrl // .targetUrl // "") | test($own) | not)
       | select((.workflowName // "") == $wf | not)
       | (.status // .state)
       | select(IN(["IN_PROGRESS","QUEUED","PENDING","WAITING","REQUESTED","EXPECTED"][]))] | length' <<<"$ROLLUP")
```

`$ROLLUP` is shell state, and the poll the branches below prescribe is necessarily its own Bash call, so the variable is gone when you come back. **Re-run that whole block — not just the `FAILED=` line — after any poll.** Both stale reads fail toward approval and neither errors: `jq` over an empty `$ROLLUP` exits 0, so `$FAILED` reads clean, and the provenance loop iterates zero times, making its "every one `cancelled`" test vacuously true.

**Don't treat a mid-flight rollup as settled.** A `FAILURE` co-existing with checks still in flight (`$PENDING > 0`) is often a *stale cancellation-cascade* artifact, not a real failure: when several events fire near-simultaneously (e.g. Dependabot opening a PR), the `tests` concurrency group cancels all but the latest, and a cancelled contributor makes an `if: always()` merge-gate omnibus (like PRQL's `check-ok-to-merge`) resolve to conclusion `FAILURE` — *not* `cancelled`, so it slips past the post-approve cancellation awareness below and reads as red. A fresh replacement run is already in flight and will re-register the omnibus. So decide on the **settled** rollup:

- **`$FAILED` set and `$PENDING > 0`** — the rollup hasn't settled. Foreground-poll until non-own checks are terminal (the Step 6 / `running-in-ci` CI-monitoring loop), then re-run the rollup block and read `$FAILED` off the fresh `$ROLLUP`. Judge the settled state, not the mid-flight snapshot — a stale cancellation-cascade `FAILURE` drops out of `$FAILED` once the replacement omnibus registers, but *only* via the reduction above; the superseded check run itself never leaves the commit.
- **`$FAILED` set and the poll cap expired with `$PENDING > 0`** — settlement is out of reach this session; a release or nightly matrix routinely outlasts the cap. Re-run the rollup block first — the loop below reads `$ROLLUP`, and the expired poll was its own Bash call. Then decide on **provenance**, not on settlement: resolve each remaining `FAILURE` to its run and read that run's own conclusion.

  ```bash
  for url in $(jq -r '.[] | select((.conclusion // .state) == "FAILURE")
                     | .detailsUrl // .targetUrl // empty' <<<"$ROLLUP"); do
    RUN=$(sed -nE 's#.*/actions/runs/([0-9]+).*#\1#p' <<<"$url")
    if [ -n "$RUN" ]; then
      gh run view "$RUN" --json conclusion --jq '.conclusion'
    else
      echo "unresolved: $url"   # third-party status context, not an Actions run
    fi
  done
  ```

  Every one `cancelled` — the red is superseded, so APPROVE and name the still-unverified checks in the body. `cancelled` is the only conclusion that earns an approval here: a real `failure`, an empty conclusion (the run is still going, so the job failed on its own merits), or an unresolvable URL (a third-party status context like `codecov/patch`, never an Actions run) all take the terminal-red branch below. Don't leave this to improvisation: the same stale red must not draw an APPROVE on one PR and a withheld approval on the next.
- **`$FAILED` set and `$PENDING == 0`** — genuine terminal red. Skip the close-out and finish at step 9. But if **no prior substantive bot review** stands on this PR, don't exit fully silent or leave only a `+1` reaction — a clean external-dependency bump then carries zero review signal. Post a brief COMMENT recording the diff assessment and why approval is held (e.g. "Diff is a correct, mechanical dependency bump; holding APPROVE because `check-ok-to-merge` is red."). Any earlier substantive review (e.g. a COMMENT with inline suggestions) already stands as the active verdict — leave it. On a bot PR where you intend to push the fix yourself (step 8), post that COMMENT before pushing, while the rollup it describes is still the current one.
- **`$FAILED` empty** — proceed with APPROVE.

Step 6's "approve, foreground-poll CI, dismiss if a check fails" pattern only recovers while the session is still alive — the job timeout or a poll cap can leave a post-approve failure undismissed and the PR carrying a misleading APPROVED state. A synchronous pre-APPROVE peek catches the case where the failure is already in the rollup — including non-required checks like `codecov/patch` that an overlay treats as a merge gate. Reducing to the latest entry per name and workflow — and, when the cap expires first, checking each `FAILURE`'s run conclusion — is what keeps a superseded red from being mistaken for a real one.

Post at most one review per run. Give a verdict (**approve** or **comment**, never "request changes") when this run has something to say: a new diff-grounded finding, or an approval because the last open concern is now resolved. If the dedup rule above left nothing new and a prior unresolved bot thread still stands, post nothing; the earlier review remains the active verdict. Use `gh pr review` for reviews, not `gh pr comment`. Note: `--comment` requires a non-empty body — if there's nothing to say and no prior concern stands, use the approve-with-empty-body pattern.

**Inline suggestions are mandatory for concrete fixes.** Whenever there's a concrete fix (typos, doc updates, naming, missing imports, minor refactors, test additions), post it as an inline suggestion on the exact line — never as a code block in the review body. Inline suggestions let the author apply with one click; code blocks force them to find the line and copy-paste manually.

For fixes targeting lines outside the diff, offer to push a fix commit instead.

Post inline suggestions via the review API:

`````bash
cat > /tmp/review-body.md << 'EOF'
Summary of suggestions
EOF

cat > /tmp/review-payload.json << 'ENDJSON'
{
  "event": "COMMENT",
  "comments": [
    {
      "path": "example/file.txt",
      "line": 3,
      "body": "```suggestion\nnew text here\n```"
    }
  ]
}
ENDJSON

BODY=$(cat /tmp/review-body.md)
jq --arg body "$BODY" '.body = $body' /tmp/review-payload.json > /tmp/review-final.json

gh api "repos/$REPO/pulls/<number>/reviews" \
  --method POST \
  --input /tmp/review-final.json
`````

**Do not** use `-f 'comments[0][path]=...'` flag syntax — `gh api` converts array indices to object keys, which GitHub rejects.

- If a review has both suggestions and prose observations, put the suggestions as inline comments and the prose in the review body.
- Multi-line suggestions: set `start_line` and `line` to define the range. GitHub **replaces** every line in that range with the suggestion content — any line in the range that isn't reproduced in the replacement is **deleted**.

  **Before posting any multi-line suggestion, verify it:**

  1. **Read the exact lines** `start_line` through `line` from the diff hunk.
  2. **Diff mentally**: every line in that range must either appear (possibly modified) in the replacement text, or be a line you intend to delete. If any line would be silently dropped, **shrink the range** or include the line in the replacement.
  3. **Cap the range at ~10 lines.** Larger suggestions are error-prone and hard to review. For changes spanning more than 10 lines, split into multiple suggestions or push a fix commit instead.
  4. **Never span markdown fences.** If the range includes a `` ``` `` line, GitHub's suggestion parser may consume it as a delimiter, corrupting the result. Either shrink the range to avoid the fence or push a commit.

#### Recovering from inline comment 422 errors

GitHub returns `422 Unprocessable Entity` with "Line could not be resolved" when inline comment line numbers don't map to valid positions in the diff. Two failure modes produce the same error message but differ in whether a review record is persisted:

- **(a) Large / complex diff**: the body is persisted first, then the inline comments are rejected — leaving an **orphan body-only review** on the PR. A blind retry creates a duplicate.
- **(b) Line outside the diff entirely**: the entire POST is rejected up front — **no review is persisted**. Retrying without inline comments is correct; editing a non-existent review will fail.

**Check which case you are in before deciding how to recover** — query for an orphan review on the current HEAD first, then branch on the result.

```bash
# The body-length test is what distinguishes an orphan from a synthetic reply
# container sitting on the same HEAD: case (a) persists the body, a container
# never has one. Without it, the PUT below would overwrite an unrelated reply.
# `gh api --paginate` applies `--jq` to each page separately, so a `| last`
# inside `--jq` returns one id *per page* — pipe to `jq -rs 'add | …'` to reduce
# over the merged array instead.
#
# The force-push filter from the pre-post guard is required here too, and the
# consequence of omitting it is worse than a skipped run: a body-bearing review
# from before a rewrite reports `.commit_id == $HEAD_SHA`, passes the body-length
# test, and the PUT below overwrites that older review's text with this run's
# findings — destroying a published review and leaving the new body over the old
# review's inline comments on code that no longer exists.
LAST_FORCE_PUSH_AT=$(gh api --paginate "repos/$REPO/issues/<number>/timeline" \
  | jq -rs 'add | [.[] | select(.event == "head_ref_force_pushed") | .created_at] | max // ""')
ORPHAN_ID=$(gh api --paginate "repos/$REPO/pulls/<number>/reviews" \
  | jq -rs --arg bot "$BOT_LOGIN" --arg head "$HEAD_SHA" --arg fp "$LAST_FORCE_PUSH_AT" \
    'add | [.[] | select(.user.login == $bot and .commit_id == $head and (.body | length) > 0)
                | select($fp == "" or .submitted_at > $fp)]
         | last | .id // empty')
```

Then, in either case, **move the failed inline comments into the review body** as fenced code blocks with file paths, and:

- **If `ORPHAN_ID` is non-empty (case a)**: edit the existing review instead of creating a duplicate.
  ```bash
  gh api "repos/$REPO/pulls/<number>/reviews/$ORPHAN_ID" \
    -X PUT -F body=@/tmp/updated-review-body.md
  ```
  If the edit itself fails, **do not post another review** — the body-only review is sufficient.

- **If `ORPHAN_ID` is empty (case b)**: retry the `POST` with `comments` omitted (body-only), since no duplicate is possible.

Prevention: before writing any inline comment, verify the target line falls inside one of the PR's diff hunks. For fixes outside the diff, use the "push a fix commit" path instead of an inline suggestion (see above).

### 6. Monitor CI

If you **stayed silent** (no review posted, nothing to dismiss), skip to step 9 — there's no follow-up gated on the CI result. Don't background-poll: per `/tend-ci-runner:running-in-ci` under "End the turn only when work is shipped", the completion notification isn't reliably delivered to a CI session.

If you **approved**, the dismissal-on-failure is a gated follow-up. Foreground-poll using the recipe in `/tend-ci-runner:running-in-ci` under "CI Monitoring" (don't use `run_in_background`). If the PR head moves while polling — a new push landed — stop polling the stale commit and fold the push in per step 9; resume monitoring on the new HEAD if it earns a verdict of its own.

Then handle the outcome:

- **All required checks passed** -> done.
- **A check failed** and it's related to the PR -> post a follow-up COMMENT review with analysis and inline suggestions, then dismiss the bot's approval:
  ```bash
  # Use PUT, not POST — the dismiss endpoint requires it
  gh api "repos/$REPO/pulls/<number>/reviews/$REVIEW_ID/dismissals" \
    -X PUT -f message="CI failed — <reason>"
  ```
  Skip if already dismissed. On **human-authored PRs**, do not push fixes — post the analysis and offer to fix, then wait for the author to accept. On **bot PRs** (Dependabot, renovate, etc.), don't stop at analysis: apply the fix per step 8 so the PR can go green, since no author will act on the offer.
- **A check was cancelled** (conclusion `cancelled`) -> do nothing. Cancellations are almost always caused by concurrency groups — a new workflow run (often triggered by your own approval event) replaces the in-progress one. The replacement run will cover the cancelled checks. **Do not re-run cancelled jobs** — that creates another run that gets cancelled again, wasting time in a loop.
- **A check failed** (conclusion `failure`, not `cancelled`) and it's a transient flake (unrelated to the PR changes) ->
  1. **Re-run the failed jobs:**
     ```bash
     gh run rerun <run-id> --failed
     ```
  2. **Report the flake.** Search for an open issue about the specific flaky test. If found, append to an existing bot comment rather than posting a new one.

### 7. Resolve handled suggestions

After submitting the review, check if any unresolved bot threads have been addressed by the new changes. Resolve threads where the suggestion was applied.

**Only resolve if the substance was addressed.** Read both the suggestion and the new code — if the author took a different approach, verify its technical accuracy before resolving. "Different wording" is not "addressed" when the new wording is less accurate than the suggestion. When in doubt, leave the thread open for a human reviewer.

**Self-authored PRs are especially risky.** When the bot is both author and reviewer, there is a bias toward accepting the code's own claims. Treat self-authored thread resolution with extra skepticism — read the code and verify the claim independently rather than trusting the doc comment or commit message.

```bash
cat > /tmp/review-threads.graphql << 'GRAPHQL'
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes {
              author { login }
              path
              line
              body
            }
          }
        }
      }
    }
  }
}
GRAPHQL

REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
BOT_LOGIN=$(gh api user --jq '.login')
OWNER=$(echo "$REPO" | cut -d/ -f1)
NAME=$(echo "$REPO" | cut -d/ -f2)

gh api graphql -F query=@/tmp/review-threads.graphql \
  -f owner="$OWNER" -f repo="$NAME" -F number=<number> \
  | jq --arg bot "$BOT_LOGIN" '
    .data.repository.pullRequest.reviewThreads.nodes[]
    | select(.isResolved == false)
    | select(.comments.nodes[0].author.login == $bot)
    | {id, path: .comments.nodes[0].path, line: .comments.nodes[0].line, body: .comments.nodes[0].body}'

# Resolve a thread that has been addressed
cat > /tmp/resolve-thread.graphql << 'GRAPHQL'
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id }
  }
}
GRAPHQL

gh api graphql -F query=@/tmp/resolve-thread.graphql -f threadId="THREAD_ID"
```

Outdated comments (null line) are best-effort — skip if the original context can't be located.

### 8. Push fixes

Pushing to the branch under review fires `synchronize`, which queues a replacement run behind this session rather than cancelling it. Submit the review (step 5) and resolve threads (step 7) before pushing, so the review documents the code the fix responds to. Then treat your own push like any other fold-in (step 9): judge it with step 1's trivial-skip heuristic — you already know the increment — and poll its CI to green per `running-in-ci`'s "a pushed fix is always gated" before ending the session.

**Bot PRs** (Dependabot, renovate, etc.): There is no human author to act on feedback, so a review that only describes the fix leaves the PR red and pushes the work onto a maintainer — the opposite of the point. If you can articulate the fix, apply it: commit and push it to the PR branch. "Not a one-token change" and "more than one syntactically valid form exists" are **not** reasons to defer — pick the option most consistent with the surrounding code and the repo's existing conventions, push it, and note any alternative in the review. The only bar for deferring is that *no defensible default exists*: a genuine semantic ambiguity that needs maintainer intent, not merely a fix that took thought to derive. If the review already worked out the answer, that answer is pushable. Rebase onto the latest target branch first if the branch is behind.

**Human PRs**: Post inline suggestions first. Additionally, offer to push a commit when the fixes are mechanical and correctness is obvious. Only push after the author accepts.

```bash
gh pr checkout <number>
git add <files>
git commit -m "fix: <description>"
git push
```

### 9. Stamp examined HEADs, then re-check for a new push

Every session on an open PR ends here, whichever path it took — a posted review, an approval, a pushed fix, or silence. A PR found CLOSED or MERGED skips it: the workflow's pre-check already skips closed PRs, so a stamp there gates nothing.

**Stamp each HEAD this session fully examined**, whatever the verdict was. Fully examined means the flow above ran against that commit; a stamp on a commit you only glanced at suppresses its review, which is worse than the redundant agent boot an unstamped examination costs.

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
# HEAD_SHA: the commit this session examined — after a fold-in, re-point it at
# the folded-in commit. Never derive it from the live head here: in the race
# window a fresh push is a head this session hasn't reviewed.
gh api "repos/$REPO/statuses/$HEAD_SHA" \
  -f state=success -f context="tend-review/<number>" \
  -f description="examined by tend-review" \
  -f target_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
```

The generated workflow holds a replacement run queued by any mid-session push until this session ends; a pre-check then reads this stamp off the live HEAD and skips the agent when it's present. The context carries the PR number because one branch can be two open PRs with different bases — an examination of one must not gate the other.

**Then re-fetch the live HEAD and draft state.** If the PR left draft mode during the session, run the full non-draft flow on the current HEAD before stamping it: the `ready_for_review` run that would have done so may sit queued behind this session and be replaced by a later push's run, which the stamp would gate out. If HEAD moved, fold the push in rather than leaving it to the queued run's cold start: judge the increment with step 1's incremental logic (the trivial-skip heuristic applies), act on the result — a review per step 5, thread resolution per step 7, or silence — stamp the commit, and re-check again. After three fold-ins, end the session leaving the newest HEAD unstamped; the queued run picks it up with a fresh view.
