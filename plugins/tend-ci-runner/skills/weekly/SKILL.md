---
name: weekly
description: Weekly maintenance — updates tend workflows, reviews and merges dependency PRs.
metadata:
  internal: true
---

# Weekly Maintenance

## Step 1: Update tend workflows

Regenerate the tend workflow files and open a PR if anything changed.

```bash
uvx tend init
```

Check for changes:
```bash
git diff --name-only .github/workflows/tend-*.yaml
```

If files changed:
1. Create a branch: `git checkout -b tend/update-workflows`
2. Commit the changes: `git add .github/workflows/tend-*.yaml && git commit -m "chore: update tend workflows"`
3. Open a PR: `gh pr create --title "chore: update tend workflows" --body "Automated weekly regeneration of tend workflow files."`
4. If CI passes, merge the PR.

If no changes, continue to the next step.

## Step 2: Find dependency PRs

```bash
gh pr list --state open --json number,title,author,labels \
  --jq '.[] | select(.author.login == "dependabot[bot]" or .author.login == "renovate[bot]" or (.labels | any(.name == "dependencies")))'
```

If no dependency PRs are open, report "No dependency PRs to process" and skip to the summary.

## Step 3: For each dependency PR

1. Check CI status: `gh pr checks <number>`
2. If CI is passing, review the diff for breaking changes (major version bumps, API changes,
   deprecation warnings)
3. If the update is safe (patch/minor with green CI), approve and merge:
   ```bash
   gh pr review <number> --approve --body "Automated dependency update — CI passing, no breaking changes."
   gh pr merge <number> --squash
   ```
4. If CI is failing, comment with the failure summary and skip
5. If a major version bump, comment noting it needs manual review and skip

## Step 4: Summary

Report: workflow update status, dependency PRs processed/merged/skipped (with reasons).
