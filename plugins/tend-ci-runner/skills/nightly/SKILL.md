---
name: nightly
description: Nightly code quality sweep — resolves bot PR conflicts, reviews recent commits, surveys existing code, checks resolved issues, and updates tend workflows.
metadata:
  internal: true
---

# Nightly Code Quality Sweep

Resolve conflicts on bot PRs, review recent commits, survey a slice of existing code/docs, and
update tend workflows.

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

Run the survey script to get today's file list (rotating through the full repo over 28 days):

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nightly-survey-files.sh
```

Skip files that aren't meaningfully reviewable: lock files (`uv.lock`, `Cargo.lock`,
`package-lock.json`), binary assets, vendored dependencies, and generated files (build output,
compiled protobuf, auto-generated workflow YAML). When unsure, check the file — a quick glance is
cheaper than missing something.

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

## Step 5: Update tend workflows

Regenerate the tend workflow files and open a PR if anything changed. The
checkout's `.github/` directory may be mounted read-only under the sandbox
(protecting bots from modifying their own workflows in place), so do the
regeneration in a git worktree under `$TMPDIR`, which is writable:

```bash
git worktree add "$TMPDIR/tend-update-workflows" -b tend/update-workflows HEAD
(cd "$TMPDIR/tend-update-workflows" && uvx tend@latest init)
git -C "$TMPDIR/tend-update-workflows" status --porcelain .github/workflows
```

If `git status` shows no changes, clean up and continue:

```bash
git worktree remove "$TMPDIR/tend-update-workflows" --force
```

If files changed, commit and open the PR from the worktree:

```bash
cd "$TMPDIR/tend-update-workflows"
git add .github/workflows/tend-*.yaml
git commit -m "chore: update tend workflows"
git push -u origin tend/update-workflows
gh pr create --title "chore: update tend workflows" \
  --body "Automated nightly regeneration of tend workflow files."
cd -
git worktree remove "$TMPDIR/tend-update-workflows" --force
```

## Step 6: Fix findings

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

## Optional steps

Not run by default. Only run a step here when the project's `running-tend` skill explicitly
enables it.

### Changelog maintenance

Keep the project's changelog up to date with recent changes. The `running-tend` skill specifies the
changelog file and the branch to push to.

1. Find the changelog file. If it doesn't exist, skip — don't create one.
2. Check out the changelog branch. Create it from the default branch if it doesn't exist yet.
3. Merge the default branch into the changelog branch to stay current. If the merge conflicts,
   delete the branch, recreate it from the default branch, and start fresh.
4. Identify merged PRs and notable commits since the last entry in the changelog.
5. Draft entries matching the existing file's style and format.
6. Commit and push directly to the changelog branch — no PR needed, the branch is kept ready to
   merge for the next release.

## Step 7: Summary

Report: commits reviewed, files surveyed, findings, actions taken, assessment (clean / minor
issues / needs attention).
