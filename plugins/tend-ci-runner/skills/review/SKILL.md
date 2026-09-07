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

Before reading the diff, run the initial snapshot. It pins the open head, resolves the bot's review state, and prepares the accurate incremental when one applies:

```bash
/usr/bin/python3 -E -s \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_preflight.py" start <number>
```

On `skip`, finish. The JSON fields replace the shell variables named below;
`incremental_path` is either null or a file containing the commits and per-file
line counts authored since the last review, excluding base-branch churn.
`recovery_pending_review_id` is the one current-head private PENDING review
compatible with this review mode, or null.

After a successful `start`, run the command below only when the review pass has
actually completed and intentionally has no reader-facing review to publish:

```bash
/usr/bin/python3 -E -s \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_preflight.py" \
  complete <number>
```

This submits a marker-only COMMENT review: no reader-facing prose, but durable
state records that the pinned head was deliberately reviewed without a verdict.
When `start` captured a ready-for-review generation, the same record acknowledges
that exact generation. Never substitute a visible “looks fine” comment.

Do not run `complete` when deferring work, abandoning an unread retargeting
delta, or stopping on an error. Those outcomes leave review demand outstanding
for recovery. Review runs never acknowledge a PR notification themselves;
the notifications poll decides whether the whole conversation, including
questions and replies outside the code review, has a current outcome.

When `recovery_pending_review_id` is non-null, bypass the already-reviewed and
trivial-increment silent exits below. The private pending review has comments to
reconcile but does not count as a finalized verdict.

When `force_full_review` is true, bypass both the already-reviewed and trivial-
increment shortcuts: becoming ready asks for a full non-draft review.

If `force_pushed_since` is `true`, the commit the bot reviewed was rewritten away: ignore `last_review_sha` entirely and review `head_sha` in full. The incremental can't run either — `last_review_sha` now names the current head rather than anything the bot read, so the range is empty and every trivial-skip heuristic keyed on it under-reports. A prior `APPROVED` is re-anchored onto the rewritten head too, so it reads as an approval of code nothing reviewed — step 6's dismissal rule clears it, along with the ordinary-push case below.

Otherwise, if `last_review_sha == head_sha` and `force_full_review` is false, this commit has already been reviewed — finish without posting. The notifications poll answers later conversation questions directly; they do not reopen code-review demand on a covered head.

If the bot reviewed a previous commit (`last_review_sha` exists but differs from `head_sha`), judge what was pushed since. Read two signals, both leak-free against base-merges:

- The PR's three-dot diff — `gh pr diff <number>` (merge-base→head, the same diff step 3 uses) — for the current state. Base-merge commits never enter it.
- The **accurate incremental** in `incremental_path` — read the whole file for the trivial-skip decision below. The script excludes everything reachable from the base tip, so a base merge's own commits are not counted as new PR work.

The incremental scopes the *review*, not anything this run writes about the PR as a whole: if you also edit the PR description, scope its claims to the merge base per **Keeping PR Titles and Descriptions Current** in `/tend-ci-runner:running-in-ci`.

If `force_full_review` is false and the incremental changes are trivial, skip the full review — go directly to step 8 to resolve any bot threads addressed by the new changes. After resolving threads: if the most recent bot review was a COMMENT that flagged issues, and those issues are now addressed, submit an APPROVE with an empty body so the PR isn't left in limbo — and the author-readiness gate under step 6 applies here too, since these are the bot's own findings closing out rather than the author's. Use the recipe under step 6, which pins the commit read here. Otherwise do not submit a new review — the existing one stands. Do NOT proceed to steps 2–7; finish. Rough heuristic: changes under ~20 added+deleted lines that don't introduce new functions, types, or control flow are typically trivial.

**Commit and PR authorship do not affect review behavior.** Apply the same trivial-vs-substantive heuristic regardless of who pushed the new commits. When `tend-notifications` or `tend-ci-fix` pushes a fix to a human-authored PR, reviewing (and re-approving) the updated state is expected — the reviewer role is independent of commit authorship.

Then read all previous bot feedback and conversation:

```bash
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/bot_review_state.py" \
  feedback <number>
```

`pending_reviews` and `pending_inline_comments` are private to the bot and
belong to a review GitHub accepted but Tend did not submit. Verify their prose
and comments against the current diff and recreate every still-valid finding
in this pass's payload; do not let them trigger the ordinary “prior feedback
already covers it” silent exit.

If `recovery_pending_review_id` is non-null, rebuild or supersede that pending
review in step 6. The publication commands delete captured pending records only
at the final boundary. Do not take an ordinary prior-feedback silent exit: either
recreate every still-valid finding, or, after verifying none remain, use
`complete`. Pending comments from other review modes remain private context but
cannot satisfy this run's verdict.

**Apply the sibling-workflow dedup rule from `running-in-ci`** to both the review body and inline comments. If a prior bot comment in the conversation already covers a point — a previous review on this or an earlier commit, a `tend-mention` reply, a `tend-triage` post, anything from a tend workflow — omit it from this review and stick to diff-grounded findings. If that leaves no new diff-grounded finding on the incremental changes and the only outstanding concern is a still-unresolved thread from an earlier bot review, do not post a new review: that thread already blocks the PR, and restating "the prior thread still applies" on every push is noise. Resolve any bot threads the new commits addressed (step 8), then finish without posting. A fresh review is warranted only when the incremental diff introduces a new finding, or resolves the last open one (then approve with an empty body — the author-readiness gate under step 6 applies here too, since these are the bot's own findings closing out rather than the author's). When concurrent runs race (a new push while the first run is still responding), both see the same unanswered question — check whether a bot reply exists after the question's timestamp before answering. Address remaining unanswered questions in the review body (not via `gh pr comment`).

#### Draft mode

If `is_draft` is true, run a lighter review:

- Skip step 2 (overlap with other PRs) — landing-readiness concern, premature for WIP.
- Skip the duplication scan in step 4 — the author is still shaping the design.
- Submit as **COMMENT only**, never APPROVE. GitHub blocks approving drafts, and the author hasn't asked for a verdict yet.
- Make the review's context clear: this is feedback on work in progress, not a merge verdict, and the author can mark it ready to request the full review.
- Do not add Tend metadata to the body. The submit command uses the captured draft mode itself and strips caller-supplied Tend metadata, even if the PR becomes ready before publication.
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
- Where a doc comment and the code disagree, the finding is which one is wrong — don't suggest a test that pins the current output, which freezes behavior nobody has called intended.

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
/usr/bin/python3 -E -s \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_preflight.py" \
  submit <number> --event APPROVE
```

The submit command reads `/tmp/reviewed-head`, performs the final live-state
check, and pins the review to that commit. When an approval needs prose, write
it to `/tmp/review-body.md` and add `--body-file /tmp/review-body.md`.

If there are actionable findings, submit them as a review with inline suggestions for concrete fixes. The review is a decision surface for the author, not a record of the reviewer's work: publish only distinct points that require a change or decision, with enough mechanism and evidence to make each credible and actionable. Correct paths, unaffected behavior, verification inventory, and search history stay in the session. Follow **Reader-facing prose** in `running-in-ci` for any supporting detail.

Don't explain what the code does — the author wrote it. Don't nitpick formatting — that's what linters are for. Explain why the consequence warrants a change.

<example>
<bad reason="Reports a correct path and the reviewer's verification, but gives the author nothing to act on">

Bad:

```
The new delegation path looks correct. I also verified that the threshold logic is unchanged.
```

</bad>
<good reason="Names one remaining consequence and the change that would resolve it">

Good:

```
The failure message still names the removed local path, so it directs users to an option that no longer exists. Update it to name the delegated command.
```

</good>
</example>

**A findings review never supersedes a standing approval — dismiss it.** GitHub moves `reviewDecision` only on an `APPROVED` or a `CHANGES_REQUESTED`, so a COMMENT posted over the bot's own earlier approval leaves the PR reading as bot-approved and mergeable over the findings you just posted. How the head moved makes no difference: an ordinary push leaves the approval standing exactly as a rewrite does. So whenever this round posts a COMMENT that withholds the verdict, dismiss the approval that still decides the PR — after the review POST lands, so a failed post doesn't leave the PR with neither a verdict nor findings. Step 7 is the exception: once CI has disproved this run's approval, dismiss it before the fallible follow-up post so an API failure cannot leave the known-wrong verdict standing. Findings are the common case, but the withheld-merge-readiness COMMENT above is reachable with an approval already standing, and it leaves the same wrong signal: the PR reads bot-approved while the review names a blocker. A COMMENT that withholds nothing does not qualify; dismissing after a purely conversational reply withdraws a verdict the code still earns.

```bash
# Re-read rather than reusing step 1's blob — a whole review has passed since.
# The script no-ops once a dismissal or later CHANGES_REQUESTED has cleared it.
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/bot_review_state.py" \
  dismiss <number> "Superseded by the review on a later commit."
```

**Form your own opinion independently.** Do not factor in other reviewers' comments or approvals when deciding whether to approve — the value of this review is as an uncorrelated signal.

**When confidence is low**, go beyond checking the implementation — question the approach: "Does this bypass or duplicate an existing API?" "What does this change *not* handle?" If the design involves a judgment call, flag it for human review as a COMMENT.

**Attribute a withheld approval to whatever actually decided it.** Cite repo guidance as the reason only when you can name the file and heading that guidance lives in. When the call is your own judgment, identify the risky consequence and the human decision it needs; judgment is sufficient authority without inventing a repository policy.

**Self-authored PRs** (`author == bot_login` in step 1's JSON — compare the literal bot login string, not "authored by someone senior" or "by the repo owner"): Complete steps 2–5 — self-review catches real issues (lint failures, edge cases) and is intentionally valuable. Do NOT attempt an APPROVE — GitHub rejects self-approvals. Submit as COMMENT when there are concerns, or stay silent and skip to step 7. The self-review exists to find concerns, not to publish a clean-path verdict or proof that earlier findings were resolved. Always post a current CI failure as a COMMENT because it is itself a concern.

**Not confident enough to approve** (unfamiliar module, subtle logic): Add a `+1` reaction instead — no review needed unless there are specific observations.

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
gh api "repos/$REPO/issues/<number>/reactions" -f content="+1"
```

#### Posting mechanics

Before composing the final payload, run the preflight without a command. It checks the PR is open, re-targets onto a newer descendant head, and stops duplicate reviews:

```bash
/usr/bin/python3 -E -s \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_preflight.py" post <number>
```

On `skip`, post nothing and finish. A new candidate head prints `delta: <path>`
but does not update `/tmp/reviewed-head`. Read that entire file in chunks,
update the review, then run `post` again. Only that second successful call
accepts the delta and advances the reviewed-head pin. Do not publish or run
`complete` after only the candidate pass.

A non-zero exit from this commandless check means nothing was decided. Fix the
error and re-run it. Publish only through `review_preflight.py submit`: it
performs the same canonical-state check immediately before the API write and
never re-targets onto a head the session did not inspect. On `skip`, post
nothing. For an inline review failure, use the exact recovery ID the command
prints instead of blindly submitting again.

**A push mid-review re-targets the review.** Everything read so far still holds for the code it was read against, and the delta is the only new information — however many pushes it spans. Read it, then post against the new head:

- Review the delta to the standard step 4 sets — it is new code, and the review you post covers it. A skim is not enough.
- Run step 5 again over the updated merged tree. The second pass must see the delta before step 6 can post against the new head.
- Findings the delta left alone stand. Post them.
- Findings the delta fixed drop out. If that empties the review and the delta itself reads clean, approve the new head: an empty-body approval is a verdict here, not the absence of one.
- Defer without posting only when you can't judge the delta — it rewrites what you just reviewed, or it is a review's worth of new code in its own right. Do not run `complete`; the queued or recovery run must review the new head in full.
- Inline comments resolve against the commit the review pins, so re-verify each one against the current `gh pr diff`, which now returns the new head's. On a file the delta didn't touch, the line is unchanged and the comment stands. On one it did, move the comment to the line the code sits on now; where the line no longer falls inside a hunk, put the finding in the review body as a fenced quote with its path, as under **Recovering from inline comment 422 errors**.
- **Read both halves of the delta file as a pair.** It contains two logs in sequence: the scoped one is the author's new code; the `base merge:` lines are base merges, and are the only place they appear. The two together distinguish an empty delta from an "Update branch" click. When a `base merge:` line appears, re-verify every inline comment against the new `gh pr diff` even if the scoped log printed nothing — the merge re-scopes hunks in files the scoped delta cannot show, so the "file the delta didn't touch" shortcut above does not hold.
- **Also read `git show --cc <merge sha>` on a base merge**, for what the merge itself changed. Where it conflicted, the author's resolution is committed *inside* the merge, and both logs miss it: the scoped log excludes merge commits, and the merges line says a merge happened, not what it changed. A finding the resolution already fixed must drop out. `--cc` prints only hunks differing from every parent, so a resolution that took the base side prints nothing at all — it tells you what a merge changed, never that a merge changed nothing, which is why re-verification above is unconditional.
- Re-compose every `suggestion` block after re-targeting, reading the new content with `git show "$(cat /tmp/reviewed-head)":<path>` — the workspace still holds the tree you reviewed, so disk gives you the old lines. A suggestion carried over unchanged can revert the author's newest edit or base-merged content.

**Pin every review to the commit you read** — the submit command reads
`/tmp/reviewed-head` and overwrites any caller-supplied `commit_id`. Two things
depend on the pin. GitHub otherwise anchors the review at whatever is live when
the POST lands, so the review claims code this session never saw. And the anchor
is what step 1 reports as `last_review_sha`: pinned to the head you re-targeted
onto, the queued run finds that head already reviewed and finishes without
posting a second review of the same code.

**Before APPROVE specifically**, run the snapshot below and require its `head_sha` to equal `/tmp/reviewed-head`. The rollup is pinned to `/tmp/reviewed-head`; a head mismatch means it does not cover the live head, so post findings if you have them, otherwise finish and leave the approval to the queued run. Then inspect that rollup: if any check has reached terminal `FAILURE`, do not emit an empty-body APPROVE — the close-out reads as the bot rubber-stamping over the visibly red signal. Re-check the author-readiness gate on the same pass — a comment withholding merge readiness can land after the review began, and the conversation you read in step 1 is by now stale.

An approval you post at a re-targeted head is yours to stand behind: the queued run reads that head as reviewed and finishes, so no successor session dismisses the approval if a check goes red. Step 7's poll is the whole net — run it to terminal before ending the session.

Reduce the rollup to the **latest entry per check name and workflow** before reading it. When a concurrency-cancelled run is replaced, GitHub keeps *both* check runs on the commit, so an un-deduped scan reports the superseded `FAILURE` alongside the replacement's `SUCCESS` — and keeps reporting it forever. Key on `workflowName` as well as the name: two workflows can register the same check name, and collapsing those into one entry would hide a genuine red behind an unrelated green.

```bash
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/poll_pr_checks.py" \
  snapshot <number> "$(cat /tmp/reviewed-head)"
```

The JSON reports `head_sha`, `pending`, and `failed` for the pinned commit after
dropping this run, this workflow, and superseded check runs. When those filters
leave no external contexts, both lists are empty; that is the clean `failed`
empty branch for this pre-approval snapshot, not evidence that the repository
registered a gating check. Re-run the snapshot after any poll; no shell state
survives between calls.

**Don't treat a mid-flight rollup as settled.** A `FAILURE` co-existing with a non-empty `pending` list is often a *stale cancellation-cascade* artifact, not a real failure: when several events fire near-simultaneously (e.g. Dependabot opening a PR), the `tests` concurrency group cancels all but the latest, and a cancelled contributor makes an `if: always()` merge-gate omnibus (like PRQL's `check-ok-to-merge`) resolve to conclusion `FAILURE` — *not* `cancelled`, so it slips past the post-approve cancellation awareness below and reads as red. A fresh replacement run is already in flight and will re-register the omnibus. So decide on the **settled** rollup:

- **`failed` non-empty and `pending` non-empty** — the rollup hasn't settled. Foreground-poll until non-own checks are terminal (the Step 7 / `running-in-ci` CI-monitoring loop), then re-run the snapshot. Judge the settled state, not the mid-flight snapshot — a stale cancellation-cascade `FAILURE` drops out once the replacement omnibus registers, but *only* via the reduction above; the superseded check run itself never leaves the commit.
- **`failed` non-empty and the poll cap expired with `pending` non-empty** — settlement is out of reach this session; a release or nightly matrix routinely outlasts the cap. Re-run the snapshot first. Then decide on **provenance**, not on settlement: resolve each remaining failure URL to its run and read that run's own conclusion.

  For each snapshot URL containing `/actions/runs/<run-id>/`, inspect that run:

  ```bash
  gh run view <run-id> --json conclusion --jq '.conclusion'
  ```

  A URL without an Actions run id is unresolved third-party status, not a
  cancellation you can prove.

  Every one `cancelled` — the red is superseded, so APPROVE and name the still-unverified checks in the body. `cancelled` is the only conclusion that earns an approval here: a real `failure`, an empty conclusion (the run is still going, so the job failed on its own merits), or an unresolvable URL (a third-party status context like `codecov/patch`, never an Actions run) all take the terminal-red branch below. Don't leave this to improvisation: the same stale red must not draw an APPROVE on one PR and a withheld approval on the next.
- **`failed` non-empty and `pending` empty** — genuine terminal red. Skip the close-out and finish. But if **no prior substantive bot review** stands on this PR, don't exit fully silent or leave only a `+1` reaction — a clean external-dependency bump then carries zero review signal. Post a brief COMMENT stating the diff assessment and the failing check that withholds approval. Any earlier substantive review already stands as the active verdict — leave it. On a bot PR where you intend to push the fix yourself (step 9), post that COMMENT before pushing, while the rollup it describes is still the current one.
- **`failed` empty** — proceed with APPROVE.

Step 7's "approve, foreground-poll CI, dismiss if a check fails" pattern only recovers while the session is still alive — the job timeout or a poll cap can leave a post-approve failure undismissed and the PR carrying a misleading APPROVED state. A synchronous pre-APPROVE peek catches the case where the failure is already in the rollup — including non-required checks like `codecov/patch` that an overlay treats as a merge gate. Reducing to the latest entry per name and workflow — and, when the cap expires first, checking each `FAILURE`'s run conclusion — is what keeps a superseded red from being mistaken for a real one.

Post at most one review per run. Give a verdict (**approve** or **comment**, never "request changes") when this pass has something to say: a new diff-grounded finding, or an approval because the last open concern is now resolved. If the dedup rule above left nothing new and a prior unresolved bot thread still stands, post nothing; the earlier review remains the active verdict. Post reviews through the reviews endpoint, not `gh pr comment`. Note: a COMMENT review requires a non-empty body — if there's nothing to say and no prior concern stands, use the approve-with-empty-body pattern.

**Inline suggestions are mandatory for concrete fixes.** Whenever there's a concrete fix (typos, doc updates, naming, missing imports, minor refactors, test additions), post it as an inline suggestion on the exact line — never as a code block in the review body. Inline suggestions let the author apply with one click; code blocks force them to find the line and copy-paste manually.

For fixes targeting lines outside the diff, offer to push a fix commit instead.

Post inline suggestions via the review API. First compose `/tmp/review-body.md` according to this step's review goal, then build the payload:

`````bash
cat > /tmp/review-payload.json << 'ENDJSON'
{
  "comments": [
    {
      "path": "example/file.txt",
      "line": 3,
      "body": "```suggestion\nnew text here\n```"
    }
  ]
}
ENDJSON

BODY=$(cat /tmp/review-body.md) || exit 0
jq --arg body "$BODY" '.body = $body' \
  /tmp/review-payload.json > /tmp/review-final.json

/usr/bin/python3 -E -s \
  "${CLAUDE_PLUGIN_ROOT}/scripts/review_preflight.py" \
  submit <number> --event COMMENT --payload-file /tmp/review-final.json
`````

**Do not** use `-f 'comments[0][path]=...'` flag syntax — `gh api` converts array indices to object keys, which GitHub rejects.

- If a review has both suggestions and prose observations, put the suggestions as inline comments and the prose in the review body.
- Multi-line suggestions: set `start_line` and `line` to define the range. GitHub **replaces** every line in that range with the suggestion content — any line in the range that isn't reproduced in the replacement is **deleted**.

  **Before posting any multi-line suggestion, verify it:**

  1. **Read the exact lines** `start_line` through `line` from the diff hunk.
  2. **Diff mentally**: every line in that range must either appear (possibly modified) in the replacement text, or be a line you intend to delete. If any line would be silently dropped, **shrink the range** or include the line in the replacement.
  3. **Cap the range at ~10 lines.** Larger suggestions are error-prone and hard to review. For changes spanning more than 10 lines, split into multiple suggestions or push a fix commit instead.
  4. **Never span markdown fences.** If the range includes a `` ``` `` line, GitHub's suggestion parser may consume it as a delimiter, corrupting the result. Either shrink the range to avoid the fence or push a commit.

#### Recovering from inline comment errors

The submit command creates inline feedback as a native PENDING review, which is
visible only to the bot, and submits it only after GitHub accepts the complete
body and comment set. Pending records never count as coverage, readiness
acknowledgment, or reader-facing feedback.

When inline submission fails, look for this exact status line:

```text
recover: pending review <id>
```

If it appears, stop this run. The next serialized review pass receives that
literal ID and the private body/comments through `start` and `feedback`,
revalidates them, and rebuilds the complete review. Do not submit a second
review in this run.

If the command instead prints `uncertain: review submission outcome unknown`,
do not submit again: GitHub may have accepted the review even though the
follow-up lookup could not observe it. Stop this run. The next PR event or the
daily `review-runs` live-work reconciliation re-enters the normal review path,
which reconstructs whether a pending review exists and rebuilds or supersedes
the still-uncovered review.

If submitting a pending review fails, do not create another review. Its native
PENDING state keeps the body and comments private and out of coverage until a
serialized recovery pass rebuilds it.

Prevention: before writing any inline comment, verify the target line falls inside one of the PR's diff hunks. For fixes outside the diff, use the "push a fix commit" path instead of an inline suggestion (see above).

### 7. Monitor CI

If you **stayed silent** (no review posted, nothing to dismiss), finish — there's no follow-up gated on the CI result. Don't background-poll: per `/tend-ci-runner:running-in-ci` under "End the turn only when work is shipped", the completion notification isn't reliably delivered to a CI session.

If you **approved**, the dismissal-on-failure is a gated follow-up. Foreground-poll using the recipe in `/tend-ci-runner:running-in-ci` under "CI Monitoring" (don't use `run_in_background`). If the PR head moves while polling, stop polling the stale commit; the queued review handles the new HEAD.

Then handle the outcome:

- **All required checks passed** -> done.
- **A check failed** and it's related to the PR -> dismiss the bot's approval,
  then post a follow-up COMMENT review with analysis and inline suggestions.
  Dismissing first removes the known-wrong verdict and its review coverage, so
  any failed COMMENT submission remains recoverable through the ordinary queue:

  ```bash
  uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/bot_review_state.py" \
    dismiss <number> "CI failed — <reason>"
  ```

  Then submit the COMMENT through step 6's standard body or inline-payload path.
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
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/bot_review_state.py" \
  threads <number>
# For each thread whose substance you verified as addressed:
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/bot_review_state.py" \
  resolve-thread <thread-id>
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
