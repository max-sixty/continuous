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
read -r HEAD_SHA PR_STATE < <(gh pr view <number> --json headRefOid,state \
  --jq '"\(.headRefOid) \(.state)"')
[ "$PR_STATE" != "OPEN" ] && echo "PR is $PR_STATE — skipping" && exit 0
# The commit this session reviewed, on disk because every posting recipe pins
# it and none of them runs in this Bash call. Posting mechanics rewrites it if
# HEAD moves mid-review.
echo "$HEAD_SHA" > /tmp/reviewed-head
PR_AUTHOR=$(gh pr view <number> --json author --jq '.author.login')
IS_DRAFT=$(gh pr view <number> --json isDraft --jq '.isDraft')
EVENT_ACTION=$(jq -r '.action // ""' < "${GITHUB_EVENT_PATH:-/dev/null}" 2>/dev/null)
if [ "$EVENT_ACTION" = "ready_for_review" ]; then
  FORCE_FULL_REVIEW=true
else
  FORCE_FULL_REVIEW=false
fi

# Which of the bot's reviews actually anchors this head — reply containers and
# force-push re-anchoring both discounted. See the script header for why
# `.commit_id` alone can't be read directly.
STATE=$(${CLAUDE_PLUGIN_ROOT}/scripts/bot-review-state.sh <number>)
LAST_REVIEW_SHA=$(jq -r '.last_substantive.sha // empty' <<<"$STATE")
FORCE_PUSHED=$(jq -r '.force_pushed_since' <<<"$STATE")
```

When `FORCE_FULL_REVIEW` is true, bypass both the already-reviewed and trivial-
increment shortcuts: becoming ready asks for a full non-draft review.

If `FORCE_PUSHED` is `true`, the commit the bot reviewed was rewritten away: ignore `LAST_REVIEW_SHA` entirely and review `HEAD_SHA` in full. The incremental below can't run either — `LAST_REVIEW_SHA` now names the current head rather than anything the bot read, so `LAST_REVIEW_SHA..HEAD_SHA` is empty and every trivial-skip heuristic keyed on it under-reports. A prior `APPROVED` is re-anchored onto the rewritten head too, so it reads as an approval of code nothing reviewed — step 6's dismissal rule clears it, along with the ordinary-push case below.

Otherwise, if `LAST_REVIEW_SHA == HEAD_SHA` and `FORCE_FULL_REVIEW` is false, this commit has already been reviewed — finish without posting. An unanswered conversation question directed at the bot (check below) is the exception: proceed so the review can answer it.

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

The incremental scopes the *review*, not anything this run writes about the PR as a whole: if you also edit the PR description, scope its claims to the merge base per **Keeping PR Titles and Descriptions Current** in `/tend-ci-runner:running-in-ci`.

If `FORCE_FULL_REVIEW` is false and the incremental changes are trivial, skip the full review — go directly to step 8 to resolve any bot threads addressed by the new changes. After resolving threads: if the most recent bot review was a COMMENT that flagged issues, and those issues are now addressed, submit an APPROVE with an empty body so the PR isn't left in limbo — and the author-readiness gate under step 6 applies here too, since these are the bot's own findings closing out rather than the author's. Use the recipe under step 6, which pins the commit read here. Otherwise do not submit a new review — the existing one stands. Do NOT proceed to steps 2–7; finish. Rough heuristic: changes under ~20 added+deleted lines that don't introduce new functions, types, or control flow are typically trivial.

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

**Apply the sibling-workflow dedup rule from `running-in-ci`** to both the review body and inline comments. If a prior bot comment in the conversation already covers a point — a previous review on this or an earlier commit, a `tend-mention` reply, a `tend-triage` post, anything from a tend workflow — omit it from this review and stick to diff-grounded findings. If that leaves no new diff-grounded finding on the incremental changes and the only outstanding concern is a still-unresolved thread from an earlier bot review, do not post a new review: that thread already blocks the PR, and restating "the prior thread still applies" on every push is noise. Resolve any bot threads the new commits addressed (step 8), then finish without posting. A fresh review is warranted only when the incremental diff introduces a new finding, or resolves the last open one (then approve with an empty body — the author-readiness gate under step 6 applies here too, since these are the bot's own findings closing out rather than the author's). When concurrent runs race (a new push while the first run is still responding), both see the same unanswered question — check whether a bot reply exists after the question's timestamp before answering. Address remaining unanswered questions in the review body (not via `gh pr comment`).

#### Draft mode

If `IS_DRAFT == "true"`, run a lighter review:

- Skip step 2 (overlap with other PRs) — landing-readiness concern, premature for WIP.
- Skip the duplication scan in step 4 — the author is still shaping the design.
- Submit as **COMMENT only**, never APPROVE. GitHub blocks approving drafts, and the author hasn't asked for a verdict yet.
- Open the review body with this exact line: `Reviewing as a draft — flagging anything that looks worth a quick fix. Mark ready for a full review.`
- Skip step 7 (CI monitoring) — drafts churn; CI failures are the author's to chase.
- Skip step 9 (push fixes) — never push to a WIP branch.

Steps 1, 3, 4 (without duplication scan), 5, 6 (COMMENT path), and 8 still apply. Stay silent if there's nothing actionable; don't post a "looks fine" comment.

### 2. Check for overlapping PRs

Before reading the diff, scan other open PRs for file overlap. If another PR touches the same files with a similar fix, flag it in the review so one can be closed as a duplicate.

### 3. Read and understand the change

1. Read the PR diff with `gh pr diff <number>`.
2. Before going deeper, look at the PR as a reader would — not just the code, but the shape: what files are being added/changed, and does anything look off?
3. Read the changed files in full (not just the diff) to understand context.

### 4. Review

Scale depth to the change. A docs-only PR or a mechanical rename needs a skim for correctness, not the full checklist. A new algorithm or state-management change needs trace analysis. Don't over-analyze trivial changes.

Check the project's CLAUDE.md for language-specific review criteria and conventions. Load any project-specific review skill if available.

**Code quality:**

- Is the code clear and well-structured?
- Are there simpler ways to express the same logic?
- Does it avoid unnecessary complexity, feature flags, or compatibility layers?
- In the bot's own automation (its skills, and the scheduled workflows that invoke it) and the repo's CI config: is the change worth its weight? Challenge new mechanism whose only payoff is saved compute — retries, skip-gates, caches, scheduling arithmetic — per **Weighing a Fix** in `running-in-ci`. Judge the whole change: a PR well-argued line by line can still cost more machinery than the compute it saves, and the review should say so plainly. Recommend the simple knob, or closing the PR, over refinement.

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

### 5. Second pass

Run a `/tend-ci-runner:code-review` pass over the PR's merged tree. Every review that reaches this step runs one — trivial diffs included; step 4's depth-scaling sets how deep the pass goes, never whether it happens. It's a structured second pass — correctness and cleanup angles, then a verify pass — that returns findings rather than posting anything, and it supplements step 4's manual checks rather than replacing them.

Scale its depth to how core the change is:

- Peripheral or mechanical (config, dependency bumps, test-only, docs that don't assert how the code behaves): tell it the change is peripheral, so it runs the short angle set in one pass.
- The project's core logic, or prose asserting how it behaves: tell it the change is core, so it fans the angles out and sweeps for gaps. Prose is checked by reading the code it describes, so a one-line Markdown diff can still be core.

What counts as core is repo-specific; let the project's own guidance (CLAUDE.md, a repo review skill) or your judgment decide. Both passes feed one verdict: fold its findings into the review you submit in step 6. It only reports back — it never posts a review, comment, or commit of its own, so the dedup and single-review path is preserved. Its findings are not the review: when it returns, continue to step 6.

### 6. Submit

**For a review that reached step 5, before submitting, say what that pass returned** — its confirmed findings, or "no findings". That statement is a compliance check: say it in the session, not the review body, so an empty-body APPROVE stays empty. The findings themselves still get folded into the review, per step 5. If you can't say, the pass didn't run — go back to step 5 and run it. A full review that reaches this point without it is not submittable. Step 1's trivial-increment and dedup close-out paths deliberately skip steps 2–7 and are exempt.

**If there are no issues, approve with an empty body — silence means correct.**

**Unless the author withheld merge readiness.** When the PR body — or a later comment from the author or a maintainer — says the change should not merge yet — "should not merge until…", "not ready", design questions the author calls unresolved — the verdict is withheld the same way the draft flag withholds it, and plenty of contributors state it in prose rather than toggling draft. Submit COMMENT instead, naming the stated blocker that holds the verdict; name it once, and on a later pass that finds nothing new stay silent rather than restating it — the surrounding dedup rules are keyed on threads, so they don't reach a body-only COMMENT. Your own findings being closed out does not clear it: "everything the reviewer raised is fixed" and "the author says this must not merge" are independent conditions, and only whoever stated the blocker retracts it. The asymmetry is why this is worth a condition: withholding a warranted approval costs a re-review on the next push, while an APPROVE standing on a PR its author gated is a wrong outward signal that persists until someone notices.

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
# Read the sha first and bail if it isn't there: inlined as `$(cat ...)` a
# missing file substitutes the empty string and the POST still runs, which is
# the unpinned review this pins against.
REVIEWED=$(cat /tmp/reviewed-head) || exit 0
${CLAUDE_PLUGIN_ROOT}/scripts/review-preflight.sh <number> -- \
  gh api "repos/$REPO/pulls/<number>/reviews" --method POST \
    -f event=APPROVE -f commit_id="$REVIEWED" -f body=""
```

`/tmp/reviewed-head` holds the commit this session reviewed — written in step 1, rewritten by **Posting mechanics** if HEAD moved. Every path that posts a review reads it back; see **Pin every review to the commit you read**.

If there are actionable findings, submit as a review with inline suggestions for concrete fixes. Every comment must give the author something to act on:

| Don't post (internal analysis) | Post (actionable) |
|---|---|
| "The fix correctly delegates to X" | "The error message still references the old behavior" |
| "The threshold logic is correct" | _(nothing — silence means correct)_ |

Don't explain what the code does — the author wrote it. Don't nitpick formatting — that's what linters are for. Explain *why* something should change, not just *what*.

**A findings review never supersedes a standing approval — dismiss it.** GitHub moves `reviewDecision` only on an `APPROVED` or a `CHANGES_REQUESTED`, so a COMMENT posted over the bot's own earlier approval leaves the PR reading as bot-approved and mergeable over the findings you just posted. How the head moved makes no difference: an ordinary push leaves the approval standing exactly as a rewrite does. So whenever this round posts a COMMENT rather than an approval, dismiss the approval that still decides the PR — after the review POST lands, so a failed post doesn't leave the PR with neither a verdict nor findings. Findings are the common case, but the withheld-merge-readiness COMMENT above is reachable with an approval already standing, and it leaves the same wrong signal: the PR reads bot-approved while the review names a blocker.

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
# Re-read rather than reusing step 1's blob — a whole review has passed since.
# `standing_approval_id` is the approval currently deciding the PR, "" when a
# dismissal or a later CHANGES_REQUESTED already cleared it, so a second run
# over the same PR dismisses nothing.
STANDING=$(${CLAUDE_PLUGIN_ROOT}/scripts/bot-review-state.sh <number> | jq -r '.standing_approval_id')
if [ -n "$STANDING" ]; then
  # PUT, not POST — the dismiss endpoint requires it.
  gh api "repos/$REPO/pulls/<number>/reviews/$STANDING/dismissals" \
    -X PUT -f message="Superseded by the review on a later commit."
fi
```

**Form your own opinion independently.** Do not factor in other reviewers' comments or approvals when deciding whether to approve — the value of this review is as an uncorrelated signal.

**When confidence is low**, go beyond checking the implementation — question the approach: "Does this bypass or duplicate an existing API?" "What does this change *not* handle?" If the design involves a judgment call, flag it for human review as a COMMENT.

**Attribute a withheld approval to whatever actually decided it.** Cite repo guidance as the reason only when you can name the file and heading that guidance lives in; a reader who goes looking for "this repo's review policy" and finds nothing written starts doubting the rest of the review too. When the call is your own judgment, say so plainly — "I'd want a human on this one, it changes what `--force` will delete" rather than "per this repo's review policy this is a hold-for-human surface". Judgment is a sufficient reason on its own; borrowed authority that doesn't exist is not.

**Self-authored PRs** (`PR_AUTHOR == BOT_LOGIN` — compare the literal bot login string, not "authored by someone senior" or "by the repo owner"): Complete steps 2–5 — self-review catches real issues (lint failures, edge cases) and is intentionally valuable. Do NOT attempt an APPROVE — GitHub rejects self-approvals. Submit as COMMENT when there are concerns, or stay silent and skip to step 7. Always post CI failure analysis as a COMMENT, even on self-authored PRs.

**Not confident enough to approve** (unfamiliar module, subtle logic): Add a `+1` reaction instead — no review needed unless there are specific observations.

```bash
gh api "repos/$REPO/issues/<number>/reactions" -f content="+1"
```

#### Posting mechanics

Before composing the final payload, run the preflight without a command. It checks the PR is open, re-targets onto a newer descendant head, and stops duplicate reviews:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/review-preflight.sh <number>
```

On `skip`, post nothing and finish. A re-targeted result also prints `delta: <path>` and updates `/tmp/reviewed-head`. Read that entire file in chunks, update the review, then run the preflight again. Do not post from the re-targeting pass.

A non-zero exit from this commandless check means nothing was decided. Fix the
error and re-run it. In command mode below, `post:` means the outward command
was attempted; handle that command's failure directly. For a review POST that
returns 422, use the orphan-aware recovery procedure below instead of blindly
rerunning the preflight.

Every review POST below passes its `gh api` command to the preflight after `--`. In that mode the preflight does not re-target: it runs the command only when two live snapshots and `bot-review-state.sh` agree that the pinned head is open and unreviewed. This keeps the final check beside the outward action.

**A push mid-review re-targets the review.** Everything read so far still holds for the code it was read against, and the delta is the only new information — however many pushes it spans. Read it, then post against the new head:

- Review the delta to the standard step 4 sets — it is new code, and the review you post covers it. A skim is not enough.
- Run step 5 again over the updated merged tree. The second pass must see the delta before step 6 can post against the new head.
- Findings the delta left alone stand. Post them.
- Findings the delta fixed drop out. If that empties the review and the delta itself reads clean, approve the new head: an empty-body approval is a verdict here, not the absence of one.
- Finish without posting only when you can't judge the delta — it rewrites what you just reviewed, or it is a review's worth of new code in its own right. The queued run then reviews the new head in full.
- Inline comments resolve against the commit the review pins, so re-verify each one against the current `gh pr diff`, which now returns the new head's. On a file the delta didn't touch, the line is unchanged and the comment stands. On one it did, move the comment to the line the code sits on now; where the line no longer falls inside a hunk, put the finding in the review body as a fenced quote with its path, as under **Recovering from inline comment 422 errors**.
- **Read both halves of the delta file as a pair.** It contains two logs in sequence: the scoped one is the author's new code; the `base merge:` lines are base merges, and are the only place they appear. The two together distinguish an empty delta from an "Update branch" click. When a `base merge:` line appears, re-verify every inline comment against the new `gh pr diff` even if the scoped log printed nothing — the merge re-scopes hunks in files the scoped delta cannot show, so the "file the delta didn't touch" shortcut above does not hold.
- **Also read `git show --cc <merge sha>` on a base merge**, for what the merge itself changed. Where it conflicted, the author's resolution is committed *inside* the merge, and both logs miss it: the scoped log excludes merge commits, and the merges line says a merge happened, not what it changed. A finding the resolution already fixed must drop out. `--cc` prints only hunks differing from every parent, so a resolution that took the base side prints nothing at all — it tells you what a merge changed, never that a merge changed nothing, which is why re-verification above is unconditional.
- Re-compose every `suggestion` block after re-targeting, reading the new content with `git show "$(cat /tmp/reviewed-head)":<path>` — the workspace still holds the tree you reviewed, so disk gives you the old lines. A suggestion carried over unchanged can revert the author's newest edit or base-merged content.

**Pin every review to the commit you read** — `commit_id` in every posting recipe, read back from `/tmp/reviewed-head`. Two things depend on the pin. GitHub otherwise anchors the review at whatever is live when the POST lands, so the review claims code this session never saw. And the anchor is what `bot-review-state.sh` reports as `LAST_REVIEW_SHA`: pinned to the head you re-targeted onto, the queued run's step 1 finds that head already reviewed and finishes without posting a second review of the same code.

**Before APPROVE specifically**, re-read live HEAD and require it to still equal `/tmp/reviewed-head`. The rollup below is the live head's, so once they differ it is not the pinned commit's rollup and the red-check gate is reading the wrong commit; post findings if you have them, otherwise finish and leave the approval to the queued run. Then peek that rollup: if any check has reached terminal `FAILURE`, do not emit an empty-body APPROVE — the close-out reads as the bot rubber-stamping over the visibly red signal. Re-check the author-readiness gate on the same pass — a comment withholding merge readiness can land after the review began, and the conversation you read in step 1 is by now stale.

An approval you post at a re-targeted head is yours to stand behind: the queued run reads that head as reviewed and finishes, so no successor session dismisses the approval if a check goes red. Step 7's poll is the whole net — run it to terminal before ending the session.

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

- **`$FAILED` set and `$PENDING > 0`** — the rollup hasn't settled. Foreground-poll until non-own checks are terminal (the Step 7 / `running-in-ci` CI-monitoring loop), then re-run the rollup block and read `$FAILED` off the fresh `$ROLLUP`. Judge the settled state, not the mid-flight snapshot — a stale cancellation-cascade `FAILURE` drops out of `$FAILED` once the replacement omnibus registers, but *only* via the reduction above; the superseded check run itself never leaves the commit.
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
- **`$FAILED` set and `$PENDING == 0`** — genuine terminal red. Skip the close-out and finish. But if **no prior substantive bot review** stands on this PR, don't exit fully silent or leave only a `+1` reaction — a clean external-dependency bump then carries zero review signal. Post a brief COMMENT recording the diff assessment and why approval is held (e.g. "Diff is a correct, mechanical dependency bump; holding APPROVE because `check-ok-to-merge` is red."). Any earlier substantive review (e.g. a COMMENT with inline suggestions) already stands as the active verdict — leave it. On a bot PR where you intend to push the fix yourself (step 9), post that COMMENT before pushing, while the rollup it describes is still the current one.
- **`$FAILED` empty** — proceed with APPROVE.

Step 7's "approve, foreground-poll CI, dismiss if a check fails" pattern only recovers while the session is still alive — the job timeout or a poll cap can leave a post-approve failure undismissed and the PR carrying a misleading APPROVED state. A synchronous pre-APPROVE peek catches the case where the failure is already in the rollup — including non-required checks like `codecov/patch` that an overlay treats as a merge gate. Reducing to the latest entry per name and workflow — and, when the cap expires first, checking each `FAILURE`'s run conclusion — is what keeps a superseded red from being mistaken for a real one.

Post at most one review per run. Give a verdict (**approve** or **comment**, never "request changes") when this pass has something to say: a new diff-grounded finding, or an approval because the last open concern is now resolved. If the dedup rule above left nothing new and a prior unresolved bot thread still stands, post nothing; the earlier review remains the active verdict. Post reviews through the reviews endpoint, not `gh pr comment`. Note: a COMMENT review requires a non-empty body — if there's nothing to say and no prior concern stands, use the approve-with-empty-body pattern.

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
REVIEWED=$(cat /tmp/reviewed-head) || exit 0
jq --arg body "$BODY" --arg sha "$REVIEWED" \
  '.body = $body | .commit_id = $sha' /tmp/review-payload.json > /tmp/review-final.json

${CLAUDE_PLUGIN_ROOT}/scripts/review-preflight.sh <number> -- \
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
# `orphan_id` is the body-bearing bot review anchored here, and only that: a
# synthetic reply container on the same HEAD has no body, so the PUT below
# can't overwrite an unrelated reply, and a body-bearing review from before a
# rewrite is excluded too — it reports `.commit_id == $HEAD_SHA`, so without
# that filter the PUT destroys a published review, leaving this run's findings
# over the old review's inline comments on code that no longer exists.
ORPHAN_ID=$(${CLAUDE_PLUGIN_ROOT}/scripts/bot-review-state.sh <number> \
  | jq -r '.orphan_id // empty')
```

Then, in either case, **move the failed inline comments into the review body** as fenced code blocks with file paths, and:

- **If `ORPHAN_ID` is non-empty (case a)**: edit the existing review instead of creating a duplicate.
  ```bash
  ${CLAUDE_PLUGIN_ROOT}/scripts/review-preflight.sh <number> \
    --edit-review "$ORPHAN_ID" -- \
    gh api "repos/$REPO/pulls/<number>/reviews/$ORPHAN_ID" \
      -X PUT -F body=@/tmp/updated-review-body.md
  ```
  If the edit itself fails, **do not post another review** — the body-only review is sufficient.

- **If `ORPHAN_ID` is empty (case b)**: retry the `POST` with `comments` omitted (body-only), since no duplicate is possible.
  ```bash
  jq 'del(.comments)' /tmp/review-final.json > /tmp/review-body-only.json
  ${CLAUDE_PLUGIN_ROOT}/scripts/review-preflight.sh <number> -- \
    gh api "repos/$REPO/pulls/<number>/reviews" \
      --method POST --input /tmp/review-body-only.json
  ```

Prevention: before writing any inline comment, verify the target line falls inside one of the PR's diff hunks. For fixes outside the diff, use the "push a fix commit" path instead of an inline suggestion (see above).

### 7. Monitor CI

If you **stayed silent** (no review posted, nothing to dismiss), finish — there's no follow-up gated on the CI result. Don't background-poll: per `/tend-ci-runner:running-in-ci` under "End the turn only when work is shipped", the completion notification isn't reliably delivered to a CI session.

If you **approved**, the dismissal-on-failure is a gated follow-up. Foreground-poll using the recipe in `/tend-ci-runner:running-in-ci` under "CI Monitoring" (don't use `run_in_background`). If the PR head moves while polling, stop polling the stale commit; the queued review handles the new HEAD.

Then handle the outcome:

- **All required checks passed** -> done.
- **A check failed** and it's related to the PR -> post a follow-up COMMENT review with analysis and inline suggestions, then dismiss the bot's approval:
  ```bash
  # Same field step 6 dismisses on: the approval this session posted is the one
  # now deciding the PR, and it reads "" once dismissed, so a rerun is a no-op.
  STANDING=$(${CLAUDE_PLUGIN_ROOT}/scripts/bot-review-state.sh <number> | jq -r '.standing_approval_id')
  # Use PUT, not POST — the dismiss endpoint requires it
  [ -z "$STANDING" ] || gh api "repos/$REPO/pulls/<number>/reviews/$STANDING/dismissals" \
    -X PUT -f message="CI failed — <reason>"
  ```
  On **human-authored PRs**, do not push fixes — post the analysis and offer to fix, then wait for the author to accept. On **third-party bot PRs** (Dependabot, renovate, etc.), don't stop at analysis: apply the fix per step 9 so the PR can go green, since no author will act on the offer. On PRs this bot authored, step 9's rule holds: the follow-up COMMENT review dispatches the author session, which applies the fix.
- **A check was cancelled** (conclusion `cancelled`) -> do nothing. Cancellations are almost always caused by concurrency groups — a new workflow run (often triggered by your own approval event) replaces the in-progress one. The replacement run will cover the cancelled checks. **Do not re-run cancelled jobs** — that creates another run that gets cancelled again, wasting time in a loop.
- **A check failed** (conclusion `failure`, not `cancelled`) and it's a transient flake (unrelated to the PR changes) ->
  1. **Re-run the failed jobs:**
     ```bash
     gh run rerun <run-id> --failed
     ```
  2. **Report the flake.** Search for an open issue about the specific flaky test. If found, append to an existing bot comment rather than posting a new one.

### 8. Resolve handled suggestions

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

### 9. Push fixes

Pushing to the branch under review fires `synchronize`, which queues another run behind this session rather than cancelling it. Submit the review (step 6) and resolve threads (step 8) before pushing, so the review documents the code the fix responds to. Poll the pushed fix's CI to green per `running-in-ci`'s "a pushed fix is always gated" before ending the session; the queued run reviews the new HEAD.

**Third-party bot PRs** (Dependabot, renovate, etc.): There is no author of any kind to act on feedback, so a review that only describes the fix leaves the PR red and pushes the work onto a maintainer — the opposite of the point. If you can articulate the fix, apply it: commit and push it to the PR branch. "Not a one-token change" and "more than one syntactically valid form exists" are **not** reasons to defer — pick the option most consistent with the surrounding code and the repo's existing conventions, push it, and note any alternative in the review. The only bar for deferring is that *no defensible default exists*: a genuine semantic ambiguity that needs maintainer intent, not merely a fix that took thought to derive. If the review already worked out the answer, that answer is pushable. Rebase onto the latest target branch first if the branch is behind.

**PRs this bot authored**: submitting a review with a body or a fresh inline comment dispatches `tend-mention`, which boots as the author and is told to action the review. It boots whether or not you also push, so pushing the fix yourself only makes it boot to find the work already landed. Let the author session act — unless the repo doesn't run `tend-mention`, where no successor exists and the paragraph above applies.

**Human PRs**: Post inline suggestions first. Additionally, offer to push a commit when the fixes are mechanical and correctness is obvious. Only push after the author accepts.

```bash
gh pr checkout <number>
git add <files>
git commit -m "fix: <description>"
git push
```
