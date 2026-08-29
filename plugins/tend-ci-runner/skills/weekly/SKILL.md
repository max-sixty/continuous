---
name: weekly
description: Weekly maintenance — reviews dependency PRs and runs any repo-specific weekly tasks defined in running-tend.
metadata:
  internal: true
---

# Weekly Maintenance

## Step 0: Load environment skills

Load `/tend-ci-runner:running-in-ci` first — it contains CI security rules, review/comment formatting, and polling conventions. This skill posts approvals and comments on PRs, so those rules apply. `running-in-ci` will also load the repo's `running-tend` overlay if one exists; keep the loaded content in mind for Step 3.

## Step 1: Find dependency PRs

```bash
gh pr list --state open --limit 200 --json number,title,author,labels \
  --jq '.[] | select(.author.login == "dependabot[bot]" or .author.login == "renovate[bot]" or (.labels | any(.name == "dependencies")))'
```

If no dependency PRs are open, note "0 dependency PRs to process" and continue to Step 3 — do not exit; repo-specific weekly tasks may still be due.

## Step 2: For each dependency PR

1. Check CI status: `gh pr checks <number>`
2. If CI is passing, review the diff for breaking changes (major version bumps, API changes, deprecation warnings)
3. If the update is safe (patch/minor with green CI), check whether the bot has already approved this commit before approving — a dependabot PR open across multiple weekly runs (or already approved by `tend-review` on creation) would otherwise accumulate redundant approvals on the same `commit_id`:
   ```bash
   HEAD_SHA=$(gh pr view <number> --json headRefOid --jq '.headRefOid')

   # `fresh_approval_sha` counts only approvals the bot actually earned: a
   # force-push re-points an earlier approval's anchor at the NEW head, so a
   # raw `.commit_id` read would skip a commit nothing reviewed and leave the
   # rebased PR carrying an approval it never earned. Dependency PRs are the
   # population tend rewrites on purpose (`nightly` posts `@dependabot
   # recreate` and ticks renovate's rebase-check), so this matters here most.
   LAST_APPROVAL_SHA=$(${CLAUDE_PLUGIN_ROOT}/scripts/bot-review-state.sh <number> \
     | jq -r '.fresh_approval_sha')

   # The file is per-PR and removed on the skip path: step 2 loops over every
   # dependency PR, so a shared name would hand the next PR this one's sha.
   rm -f /tmp/checked-head-<number>
   if [ -n "$LAST_APPROVAL_SHA" ] && [ "$LAST_APPROVAL_SHA" = "$HEAD_SHA" ]; then
     echo "Already approved on this commit; skipping."
   else
     echo "$HEAD_SHA" > /tmp/checked-head-<number>
   fi
   ```

   **If that printed `skipping`, this PR is done — move to the next one.**
   Otherwise compose `/tmp/review-body.md` with the Write tool: one line naming
   the package, bump type, and what you checked, e.g. "ruff 0.13 → 0.14 (patch),
   CI green, no API changes". A file rather than an inline `--body` because a
   package name written as inline code puts a backtick in a double-quoted
   argument, and bash runs the span as a command. Then post, re-reading the sha
   from disk, since shell state didn't survive the Write:

   ```bash
   # `commit_id` pins the approval to the commit that was checked. Unpinned,
   # GitHub anchors it at whatever is live when the POST lands — an approval
   # of code nothing checked, on a PR `nightly` rebases on purpose. Reading a
   # file this PR's check did not write fails the POST rather than mispinning it.
   REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
   gh api "repos/$REPO/pulls/<number>/reviews" --method POST \
     -f event=APPROVE -f commit_id="$(cat /tmp/checked-head-<number>)" \
     -F body=@/tmp/review-body.md
   ```
4. If CI is failing, comment with the failure summary and skip
5. If a major version bump, comment noting it needs manual review and skip
6. On either skip path (4 or 5), dismiss an approval that predates the newest rewrite before you leave. Both paths are reachable *because* a rebase changed something, and neither passes through item 3's guard — so the pre-rewrite approval stays the bot's latest review, re-anchored onto the current head, and the PR still reads as bot-approved while you comment that it isn't mergeable:
   ```bash
   REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
   # `stale_approval_id` is set only when the approval currently deciding the
   # PR's state is the pre-rewrite one — never merely when some stale approval
   # exists, which would dismiss a review a later approval already superseded
   # and leave the live one untouched.
   STALE_APPROVAL_ID=$(${CLAUDE_PLUGIN_ROOT}/scripts/bot-review-state.sh <number> \
     | jq -r '.stale_approval_id')

   if [ -n "$STALE_APPROVAL_ID" ]; then
     # PUT, not POST — the dismiss endpoint requires it. Keep the message to what
     # these paths actually do: they comment and stop, so don't promise a re-review.
     gh api "repos/$REPO/pulls/<number>/reviews/$STALE_APPROVAL_ID/dismissals" \
       -X PUT -f message="Rebased since this approval; the new head is unreviewed."
   fi
   ```
   A dismissed review reports `DISMISSED` rather than `APPROVED`, so the filter stops matching it and a later run re-dismisses nothing.

## Step 3: Repo-specific weekly tasks

Perform any weekly maintenance the loaded `running-tend` overlay defines, following the repo's PR title conventions. If it defines no weekly tasks (or none are due this week), say so in the summary.

## Step 4: Summary

Report: dependency PRs processed/approved/skipped (with reasons), and repo-specific weekly tasks completed (or "no repo-specific weekly tasks defined" / "no weekly tasks due").
