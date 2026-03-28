---
name: renovate
description: Weekly dependency update — reviews open Dependabot/Renovate PRs and merges those that pass CI.
metadata:
  internal: true
---

# Weekly Dependency Update

Review and merge dependency update PRs from automated tools (Dependabot,
Renovate, etc.).

## Step 1: Find dependency PRs

```bash
gh pr list --state open --json number,title,author,labels \
  --jq '.[] | select(.author.login == "dependabot[bot]" or .author.login == "renovate[bot]" or (.labels | any(.name == "dependencies")))'
```

If no dependency PRs are open, report "No dependency PRs to process" and exit.

## Step 2: For each PR

1. Check CI status: `gh pr checks <number>`
2. If CI is passing, review the diff for breaking changes (major version bumps,
   API changes, deprecation warnings)
3. If the update is safe (patch/minor with green CI), approve and merge:
   ```bash
   gh pr review <number> --approve --body "Automated dependency update — CI passing, no breaking changes."
   gh pr merge <number> --squash
   ```
4. If CI is failing, comment with the failure summary and skip
5. If a major version bump, comment noting it needs manual review and skip

## Step 3: Summary

Report: PRs processed, merged, skipped (with reasons).
