---
name: nightly
description: Nightly code quality sweep — resolves bot PR conflicts, reviews recent commits, surveys existing code, and checks resolved issues.
metadata:
  internal: true
---

# Nightly Code Quality Sweep

Three phases: resolve conflicts on bot PRs, review recent commits, and survey a slice of existing
code/docs.

## Step 1: Resolve conflicts on bot PRs

```bash
BOT_LOGIN=$(gh api user --jq '.login')
gh pr list --author "$BOT_LOGIN" --json number,title,mergeable,headRefName \
  --jq '.[] | select(.mergeable == "CONFLICTING")'
```

For each conflicted PR, dispatch a subagent to:

1. Check out the PR: `gh pr checkout <number>`
2. Merge the default branch: `git merge origin/main`
3. Resolve conflicts (read files, understand both sides), `git add`, `git commit --no-edit`
4. Push and poll CI using the approach from `/tend-ci-runner:running-in-ci`
5. If conflicts are too complex, `git merge --abort` and comment explaining manual resolution is
   needed

Run subagents in parallel. Each must work in isolation (`git worktree add /tmp/pr-<number>
<branch>`). After all complete, clean up temp worktrees.

Skip if no PRs have conflicts.

## Step 2: Review recent commits

```bash
git log --since='24 hours ago' --oneline main
```

If no commits in the past 24 hours, skip this step.

Get the aggregate diff:

```bash
OLDEST=$(git log --since='24 hours ago' --format='%H' main | tail -1)
git diff ${OLDEST}^..HEAD
git log --since='24 hours ago' --format='%h %s' main
```

Read the project's CLAUDE.md before reviewing. Apply the review checklist below to the diff,
focusing on changes rather than unchanged code. Also check whether CLAUDE.md itself needs updating
to reflect the new code (e.g., new file paths, changed commands, removed patterns).

## Step 3: Check existing issues

```bash
gh issue list --state open --json number,title
gh pr list --state open --json number,title,headRefName
```

For each open issue, check whether recent commits or the current codebase state already resolve
it. If resolved, comment with the evidence (commits, CI runs, or code state that resolves the
issue). Only close the issue with `gh issue close` if the repo's guidance (e.g., `running-tend`
skill) explicitly authorizes closing issues. Otherwise, leave it open for a maintainer to close.

## Step 4: Rolling survey

Run the survey script to get today's file list (~10 files, rotating through the full repo over 28
days):

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/todays-survey-files.sh
```

Before reviewing files, read the project's CLAUDE.md and any project-specific skills or review
criteria it references. Apply the review checklist below to each file in full.

## Review checklist

Used by both Step 2 (applied to recent diffs) and Step 4 (applied to full files).

**General quality:**
- Bugs, logic errors, unhandled edge cases
- Dead code, unused imports, unreachable branches
- Simplification opportunities — unnecessary abstractions, indirection, or complexity
- Stale or incorrect documentation (comments, docstrings that no longer match behavior)
- Missing test coverage for non-trivial logic

**Convention compliance (from CLAUDE.md and project skills):**
- Code patterns that violate conventions stated in the project's CLAUDE.md
- Stale CLAUDE.md entries — conventions that reference renamed files, deleted functions, or
  outdated patterns
- Skills that have drifted from actual project behavior (instructions that no longer match how the
  code works)

## Step 5: Fix findings

Before acting on findings, check for duplicates and existing work:

```bash
gh issue list --state open --json number,title
gh pr list --state open --json number,title,headRefName
```

The default action is a PR, not an issue. If there's a plausible fix, make it — explain
uncertainty in the PR description.

For each finding:

1. **Create a PR** — branch, fix, run full test suite, commit, push, create PR, poll CI. **Every
   bug fix must include a regression test that would have failed before the fix.** If a test is not
   feasible (e.g., pure documentation changes), note why in the PR description. When uncertain
   about the approach, explain the trade-offs in the description.
2. **Create an issue only when there's no obvious fix** — design questions, problems needing
   maintainer input, or findings requiring investigation beyond what the survey can provide.

## Step 6: Summary

Report: commits reviewed, files surveyed, findings, actions taken, assessment (clean / minor
issues / needs attention).
