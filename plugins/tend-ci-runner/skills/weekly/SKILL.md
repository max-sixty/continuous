---
name: weekly
description: Weekly maintenance — reviews dependency PRs.
metadata:
  internal: true
---

# Weekly Maintenance

## Step 0: Load environment skills

Load `/tend-ci-runner:running-in-ci` first — it contains CI security rules, review/comment
formatting, and polling conventions. This skill posts approvals and comments on PRs, so those
rules apply.

## Step 1: Find dependency PRs

```bash
gh pr list --state open --json number,title,author,labels \
  --jq '.[] | select(.author.login == "dependabot[bot]" or .author.login == "renovate[bot]" or (.labels | any(.name == "dependencies")))'
```

If no dependency PRs are open, report "No dependency PRs to process" and skip to the summary.

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

## Step 3: Summary

Report: dependency PRs processed/approved/skipped (with reasons).
