---
name: weekly
description: Weekly maintenance — reviews dependency PRs and runs any repo-specific weekly tasks defined in running-tend.
metadata:
  internal: true
---

# Weekly Maintenance

## Step 0: Load environment skills

Load `/tend-ci-runner:running-in-ci` first — it contains CI security rules, review/comment
formatting, and polling conventions. This skill posts approvals and comments on PRs, so those
rules apply. `running-in-ci` will also load the repo's `running-tend` overlay if one exists;
keep the loaded content in mind for Step 3.

## Step 1: Find dependency PRs

```bash
gh pr list --state open --json number,title,author,labels \
  --jq '.[] | select(.author.login == "dependabot[bot]" or .author.login == "renovate[bot]" or (.labels | any(.name == "dependencies")))'
```

If no dependency PRs are open, note "0 dependency PRs to process" and continue to Step 3 —
do not exit; repo-specific weekly tasks may still be due.

## Step 2: For each dependency PR

1. Check CI status: `gh pr checks <number>`
2. If CI is passing, review the diff for breaking changes (major version bumps, API changes,
   deprecation warnings)
3. If the update is safe (patch/minor with green CI), approve:
   ```bash
   gh pr review <number> --approve --body "Automated dependency update — CI passing, no breaking changes."
   ```
4. If CI is failing, comment with the failure summary and skip
5. If a major version bump, comment noting it needs manual review and skip

## Step 3: Repo-specific weekly tasks

Scan the loaded `running-tend` skill for sections describing weekly maintenance — typical
headings include "Weekly Maintenance", "Weekly:", or task names like "MSRV bump", "toolchain
update", "cache audit", "README refresh". For each such task, perform it as the repo describes
and follow the repo's PR title conventions when opening a PR.

If `running-tend` defines no weekly tasks (or none are due this week), say so in the summary.

## Step 4: Summary

Report: dependency PRs processed/approved/skipped (with reasons), and repo-specific weekly
tasks completed (or "no repo-specific weekly tasks defined" / "no weekly tasks due").
