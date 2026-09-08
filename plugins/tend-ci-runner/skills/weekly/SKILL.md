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
3. If the update is safe (patch/minor with green CI), request the serialized
   review workflow:

   ```bash
   uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/bot_review_state.py" \
     request <number>
   ```

   The command dispatches only when the canonical reducer reports outstanding
   review demand. The review workflow owns the verdict, including head pinning,
   native dismissal context, duplicate suppression, and the final publication
   boundary. Move to the next PR whether it prints `requested` or `skip`.
4. If CI is failing, comment with the failure summary and skip
5. If a major version bump, comment noting it needs manual review and skip
6. On either skip path (4 or 5), dismiss an approval that predates the newest rewrite before you leave. Both paths are reachable *because* a rebase changed something, and neither passes through item 3's guard — so the pre-rewrite approval stays the bot's latest review, re-anchored onto the current head, and the PR still reads as bot-approved while you comment that it isn't mergeable:
   ```bash
   uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/bot_review_state.py" \
     dismiss-stale <number> \
     "Rebased since this approval; the new head is unreviewed."
   ```
   A dismissed review reports `DISMISSED` rather than `APPROVED`, so the filter stops matching it and a later run re-dismisses nothing.

## Step 3: Repo-specific weekly tasks

Perform any weekly maintenance the loaded `running-tend` overlay defines, following the repo's PR title conventions. If it defines no weekly tasks (or none are due this week), say so in the summary.

## Step 4: Summary

Report: dependency PRs processed/review-requested/skipped (with reasons), and repo-specific weekly tasks completed (or "no repo-specific weekly tasks defined" / "no weekly tasks due").
