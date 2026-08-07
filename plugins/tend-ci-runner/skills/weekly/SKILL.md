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
gh pr list --state open --json number,title,author,labels \
  --jq '.[] | select(.author.login == "dependabot[bot]" or .author.login == "renovate[bot]" or (.labels | any(.name == "dependencies")))'
```

If no dependency PRs are open, note "0 dependency PRs to process" and continue to Step 3 — do not exit; repo-specific weekly tasks may still be due.

## Step 2: For each dependency PR

1. Check CI status: `gh pr checks <number>`
2. If CI is passing, review the diff for breaking changes (major version bumps, API changes, deprecation warnings)
3. If the update is safe (patch/minor with green CI), check whether the bot has already approved this commit before approving — a dependabot PR open across multiple weekly runs (or already approved by `tend-review` on creation) would otherwise accumulate redundant approvals on the same `commit_id`:
   ```bash
   HEAD_SHA=$(gh pr view <number> --json commits --jq '.commits[-1].oid')
   BOT_LOGIN=$(gh api user --jq '.login')
   REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')

   # A force-push re-points an existing review's commit anchor at the NEW head,
   # so an approval of the pre-rebase code reports the current `$HEAD_SHA` and
   # this guard skips a commit the bot never read — leaving the rebased PR
   # carrying an approval it never earned. Dependency PRs are the population
   # tend rewrites on purpose (`nightly` posts `@dependabot recreate` and ticks
   # renovate's rebase-check), so ignore approvals older than the newest
   # rewrite. REST reviews carry `.commit_id`/`.submitted_at`, NOT the
   # `.commit.oid` that `gh pr view --json reviews` returns; `gh api --jq`
   # accepts no `--arg`, so pipe to `jq`.
   LAST_FORCE_PUSH_AT=$(gh api --paginate "repos/$REPO/issues/<number>/timeline" \
     | jq -rs 'add | [.[] | select(.event == "head_ref_force_pushed") | .created_at] | max // ""')
   LAST_APPROVAL_SHA=$(gh api --paginate "repos/$REPO/pulls/<number>/reviews" \
     | jq -rs --arg bot "$BOT_LOGIN" --arg fp "$LAST_FORCE_PUSH_AT" \
       'add | [.[] | select(.user.login == $bot and .state == "APPROVED")
                   | select($fp == "" or .submitted_at > $fp)]
            | last | .commit_id // empty')

   if [ -n "$LAST_APPROVAL_SHA" ] && [ "$LAST_APPROVAL_SHA" = "$HEAD_SHA" ]; then
     echo "Already approved on this commit; skipping."
   else
     # Compose a one-line review body naming the package, bump type, and what you
     # checked — e.g. "ruff 0.13 → 0.14 (patch), CI green, no API changes".
     gh pr review <number> --approve --body "$REVIEW_BODY"
   fi
   ```
4. If CI is failing, comment with the failure summary and skip
5. If a major version bump, comment noting it needs manual review and skip

## Step 3: Repo-specific weekly tasks

Perform any weekly maintenance the loaded `running-tend` overlay defines, following the repo's PR title conventions. If it defines no weekly tasks (or none are due this week), say so in the summary.

## Step 4: Summary

Report: dependency PRs processed/approved/skipped (with reasons), and repo-specific weekly tasks completed (or "no repo-specific weekly tasks defined" / "no weekly tasks due").
