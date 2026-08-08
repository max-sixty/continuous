---
name: nightly
description: Nightly code quality sweep — resolves bot PR conflicts, reviews recent commits, surveys existing code, checks resolved issues, and updates tend workflows.
metadata:
  internal: true
---

# Nightly Code Quality Sweep

Resolve conflicts on bot PRs, review recent commits, survey a slice of existing code/docs, and update tend workflows.

## Step 0: Load environment skills

Load `/tend-ci-runner:running-in-ci` first — it contains CI security rules,
polling conventions, and comment formatting guidance. It will also prompt you
to load any repo-specific skills (e.g., `running-tend`).

## Step 1: Verify bot PAT scopes

Run the scope audit script to check the bot PAT against tend's required classic OAuth scopes (`repo`, `workflow`, `notifications`, `write:discussion`, `gist`, `user`):

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pat-scope-audit.sh
```

The script prints `key=value` lines. Act on `STATUS`:

- `STATUS=ok`: all scopes present. Search open issues for a PAT scope audit tracking issue (`gh issue list --state open --search "PAT in:title"`); if found, close it with a comment noting the scopes are now granted.
- `STATUS=fine-grained`: no `X-OAuth-Scopes` header. Fine-grained PATs have no documented self-introspection endpoint — skip.
- `STATUS=missing`: open or update a tracking issue. Use a title containing "PAT" (e.g. `Bot PAT: missing scopes`) so future runs can dedup by title search. Before creating, run `gh issue list --state open --search "PAT in:title"` and update the existing issue with `gh issue edit` if one is already open. The body lists the values from `MISSING=`, names the secret to update (`TEND_BOT_TOKEN`), and links step 8 of the `install-tend` skill for remediation: https://github.com/max-sixty/tend/blob/main/plugins/install-tend/skills/install-tend/SKILL.md#8-bot-token-and-secret

## Step 2: Check tend configuration drift

Run `tend check` to verify this repo's tend setup (branch protection, bot
permission, and where credentials live). `tend-uvx.sh` is `uvx` backed by a
tend-private uv — the agent's PATH carries the repo's own, which tend must not
override:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/tend-uvx.sh" tend@latest check 2>&1 | tee /tmp/tend-check.txt
```

If **every** check line is `PASS` (no `FAIL` *and* no `SKIP`), close any
open drift issue. A run with only `SKIP` lines (e.g. lost API permission, a
transient `gh` error) is *not* a pass — leave the issue untouched, neither
close nor file. Scope to bot-authored issues so a maintainer-filed issue
that happens to contain "configuration drift" is never auto-closed:

```bash
gh issue list --state open --author '@me' \
  --search '"configuration drift" in:title' \
  --json number --jq '.[].number' \
  | xargs -r -I {} gh issue close {} --comment 'tend check now passes.'
```

If any check is `FAIL`, file or update **one** tracking issue with title
`tend check: configuration drift on <owner>/<repo>`. Dedup by title,
scoped to bot-authored issues:

```bash
gh issue list --state open --author '@me' \
  --search '"configuration drift" in:title' \
  --json number,title,body
```

No labels. Body lists the current `FAIL` lines (one bullet per check, with
a one-line reason) plus a `_Last refreshed: <YYYY-MM-DD>_` footer. Updates:

- **Failure set identical to the open issue** → edit body (refresh footer)
  only, no comment.
- **Failure set changed** → edit body to match current state and post a
  comment describing the delta (added/removed/changed checks).
- **No open issue** → create one.

## Step 3: Resolve conflicts on bot PRs

Find conflicted PRs from this bot and from upstream dependency bots:

Don't filter `gh pr list --json mergeable` on `== "CONFLICTING"`. `mergeable` is computed lazily: the first query after `main` moves returns `UNKNOWN` and only *enqueues* the computation, so a cold read reports a conflicted PR as clean. There is no blocking read — [the REST docs](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request) prescribe resubmitting the request until the value settles. Test-merge locally instead: `git merge-tree` answers the same question synchronously, in-process, with no retry loop.

```bash
BOT_LOGIN=$(gh api user --jq '.login')
git fetch --quiet origin main
for author in "$BOT_LOGIN" app/dependabot app/renovate; do
  out="/tmp/prs-${author//\//-}.json"   # `app/dependabot` has a slash; strip it
  # `--limit 100` is load-bearing: `gh pr list` defaults to 30 and truncates
  # silently, so PR 31 onward would never be test-merged.
  gh pr list --author "$author" --limit 100 --json number,title,headRefName,author > "$out"
  # A failed query leaves `$out` empty, which reads as "no conflicts" — the
  # same silent-miss shape. `[]` (no open PRs) is 3 bytes, a failure is 0.
  [ -s "$out" ] || { echo "query for $author never landed — conflicts unverified"; continue; }
  # One fetch for every head, forced because bot branches get force-pushed.
  # `refs/pull/N/head` also covers PRs opened from a fork.
  mapfile -t refs < <(jq -r '.[].number | "refs/pull/\(.)/head:refs/tend/pr/\(.)"' "$out")
  [ "${#refs[@]}" -eq 0 ] || git fetch --quiet --force origin "${refs[@]}"
  jq -r '.[] | "\(.number)\t\(.title)"' "$out" | while IFS=$'\t' read -r n title; do
    # Clean merge: exit 0. Conflict: non-zero with the tree OID and conflicted
    # paths on stdout. Anything else (ref never fetched, bad revision): non-zero
    # with stdout empty — so test `$tree`, not the exit status alone.
    if tree=$(git merge-tree --write-tree origin/main "refs/tend/pr/$n" 2>/dev/null); then
      continue
    elif [ -n "$tree" ]; then
      echo "CONFLICTING: $author #$n $title"
    else
      echo "merge test never ran, conflicts unverified: $author #$n $title"
    fi
  done
done
```

Skip the rest of this step only when every query landed and nothing printed. A PR whose merge test never ran is not a clean one — investigate it and report it as unverified rather than counting it clean.

### Upstream dependency bots: trigger the bot's own rebase

`dependabot[bot]` and `renovate[bot]` both attempt rebases on their own, but stop once `main` rewrites a file the bump also touched (`Cargo.lock`, `uv.lock`, generated headers). The PR then sits `CONFLICTING`. Each bot exposes a way to force a fresh rebuild against current `main`.

For each conflicted PR by one of these bots, first confirm the branch has no human commits — both triggers force-push and would discard local edits. The check compares each commit's author login against the bot's commit-author login (`dependabot[bot]` or `renovate[bot]`); note that the PR's `.author.login` is the App slug (`app/dependabot`, `app/renovate`) and does **not** match — use the literal commit-author login below.

```bash
# COMMIT_LOGIN is "dependabot[bot]" or "renovate[bot]" depending on the bot
gh pr view <number> --json commits \
  | jq --arg bot "$COMMIT_LOGIN" \
       '[.commits[].authors[].login] | unique | map(select(. != $bot))'
```

- Empty → trigger the bot per the table below.
- Non-empty → skip. Leave the PR for manual resolution; the force-push would throw away the human commits.

| `--author` (PR list) | Commit-author login | Trigger |
| --- | --- | --- |
| `app/dependabot` | `dependabot[bot]` | Post `@dependabot recreate` as a comment. |
| `app/renovate` | `renovate[bot]` | Edit the PR body and replace `- [ ] <!-- rebase-check -->` with `- [x] <!-- rebase-check -->`. Renovate has no comment command for rebase. |

Do not check out or rebase manually — the bot owns the branch and will overwrite anything you push.

### Bot-authored PRs: resolve manually

Set the git identity once in the main session **before dispatching** — the subagents below `git commit --no-edit` in fresh worktrees, and as separate invocations they don't reliably load `running-in-ci`. `--global` writes to the runner's shared `~/.gitconfig`, so the subagents inherit it and don't fail with `Author identity unknown`. See "Configure git identity before the first commit" in `/tend-ci-runner:running-in-ci`.

```bash
BOT_LOGIN=$(gh api user --jq '.login'); BOT_ID=$(gh api user --jq '.id')
git config --global user.name "$BOT_LOGIN"
git config --global user.email "${BOT_ID}+${BOT_LOGIN}@users.noreply.github.com"
```

For each conflicted PR authored by `$BOT_LOGIN`, dispatch a subagent to:

1. Check out the PR: `gh pr checkout <number>`
2. Merge the default branch: `git merge origin/main`
3. Resolve conflicts (read files, understand both sides), `git add`, `git commit --no-edit`
4. Push, then wait for CI per **CI Monitoring** in `/tend-ci-runner:running-in-ci`
5. If conflicts are too complex, `git merge --abort` and comment explaining manual resolution is needed

Run subagents in parallel. Each must work in isolation (`git worktree add /tmp/pr-<number>
<branch>`). After all complete, clean up temp worktrees.

## Step 4: Review recent commits

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

Read the project's CLAUDE.md before reviewing. Apply the review checklist below to the diff, focusing on changes rather than unchanged code. Also check whether CLAUDE.md itself needs updating to reflect the new code (e.g., new file paths, changed commands, removed patterns).

## Step 5: Check existing issues

```bash
gh issue list --state open --json number,title
gh pr list --state open --json number,title,headRefName
```

For each open issue, check whether recent commits or the current codebase state already resolve it. If resolved, comment with the evidence (commits, CI runs, or code state that resolves the issue). Close the issue with `gh issue close` when:

- The bot opened the issue itself to report a transient condition (e.g., a "Nightly tests failed" report from a prior run) and the condition has clearly resolved — the fix PR is merged and the relevant CI on `main` is passing. Skip this case where closing the issue is itself a signal rather than a record of resolution: a body containing "Do not close manually" (recurring trackers with their own lifecycle), or the `tend-rate-limit` label, where a maintainer's close is what lifts the bot past its own rate limit. Closing that one as the bot lifts nothing — the preflight counts only closes by a person — but it clears a decision still waiting on one.
- The repo's guidance (e.g., `running-tend` skill) explicitly authorizes closing issues.

Otherwise, leave it open for a maintainer to close.

### Enrich tend-outage issues

The action's "Report failure" step records only a workflow run link in `tend-outage` issues — annotations and job logs aren't reliably available while the job is in_progress. Run the enrichment script to fetch failure details for each newly referenced run and post them as a comment. The script is idempotent: it skips runs already marked with `<!-- enriched-run:RUN_ID -->`.

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/enrich-tend-outage-issues.sh"
```

## Step 6: Rolling survey

Run the survey script to get today's file list (rotating through the full repo over 28 days):

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/nightly-survey-files.sh
```

Skip files that aren't meaningfully reviewable: lock files (`uv.lock`, `Cargo.lock`, `package-lock.json`), binary assets, vendored dependencies, and generated files (build output, compiled protobuf, auto-generated workflow YAML). When unsure, check the file — a quick glance is cheaper than missing something.

Before reviewing files, read the project's CLAUDE.md and any project-specific skills or review criteria it references. Apply the review checklist below to each file in full.

## Review checklist

Used by both Step 4 (applied to recent diffs) and Step 6 (applied to full files).

**General quality:**
- Bugs, logic errors, unhandled edge cases
- Dead code, unused imports, unreachable branches
- Simplification opportunities — unnecessary abstractions, indirection, or complexity
- Stale or incorrect documentation (comments, docstrings that no longer match behavior)
- Missing test coverage for non-trivial logic

**Convention compliance (from CLAUDE.md and project skills):**
- Code patterns that violate conventions stated in the project's CLAUDE.md
- Stale CLAUDE.md entries — conventions that reference renamed files, deleted functions, or outdated patterns
- Skills that have drifted from actual project behavior (instructions that no longer match how the code works)

## Step 7: Update tend workflows

Regenerate the tend workflow files and open a PR if anything changed. The checkout's `.github/` directory may be mounted read-only under the sandbox (protecting bots from modifying their own workflows in place), so do the regeneration in a git worktree under `/tmp`, which is writable. Use the literal path `/tmp/tend-update-workflows` — GitHub Actions runners leave `$TMPDIR` unset, so a `$TMPDIR/...` path expands to an unwritable root path.

```bash
# Base the worktree on the update-workflows branch only when an **open PR**
# rides it, so the regen produces only the incremental delta. Otherwise base
# on HEAD. Gate on the open PR, not on branch-ref existence: a PR closed without
# merge leaves the branch behind, and basing on that stale branch carries the
# closed PR's content on top of main — inflating the diff and producing an
# inaccurate PR body, and defeating the no-value skip below (its "only
# non-stamp diff" test only holds when the diff is computed against the
# true base, not a stale branch's accumulated content).
# When no open PR exists, drop any leftover remote branch so the push starts
# fresh from HEAD. `-B` resets a stale local branch from a prior failed
# attempt rather than rejecting it.
git fetch origin tend/update-workflows 2>/dev/null || true
if gh pr list --head tend/update-workflows --state open --json number --jq '.[0].number' | grep -q .; then
  BASE=$(git rev-parse origin/tend/update-workflows)
else
  git push origin --delete tend/update-workflows 2>/dev/null || true
  BASE=$(git rev-parse HEAD)
fi
git worktree add "/tmp/tend-update-workflows" -B tend/update-workflows "$BASE"
cd "/tmp/tend-update-workflows"

# Capture the stamped tend version before regenerating, so the next bash
# call can report the bump. Header anchor: `# Generated by tend X.Y.Z.`
# Workflows generated before the header stamp existed return an empty
# string; the renderer below then omits the version line rather than
# printing `unknown → X.Y.Z`. Shell state doesn't persist between bash
# calls, so the version is stashed to a temp file.
grep -hoE '^# Generated by tend [0-9]+\.[0-9]+\.[0-9]+' \
  .github/workflows/tend-*.yaml 2>/dev/null \
  | sed -E 's/^# Generated by tend //' | sort -u | head -1 \
  > "/tmp/tend-old-ver"

"${CLAUDE_PLUGIN_ROOT}/scripts/tend-uvx.sh" tend@latest init
# `init` auto-migrates a legacy `.config/tend.toml` → `.yaml` if it finds
# one (verifies parsed equivalence before swapping); `.config/` is checked
# alongside `.github/workflows` so that one-shot upgrade ships in the same
# nightly PR as the regenerated workflows that depend on it.
git status --porcelain .github/workflows .config

# Stamp-only check: if the only diff is the `# Generated by tend X.Y.Z`
# header (e.g. dependabot has already bumped the action refs in a patch
# release), the workflow bodies are unchanged and the existing files are
# still accurate. A header-only PR carries no value — treat it as a no-op.
NON_STAMP_DIFF=$(git diff --no-color .github/workflows .config \
  | grep -E '^[+-]' \
  | grep -vE '^(\+\+\+|---) ' \
  | grep -vE '^[+-]# Generated by tend [0-9]+\.[0-9]+\.[0-9]+\. Regenerate with: uvx tend@latest init$' \
  | wc -l)
```

If `git status` shows no changes, or `NON_STAMP_DIFF` is `0`, clean up
and skip the PR:

```bash
cd -
git worktree remove "/tmp/tend-update-workflows" --force
```

If files changed, detect the version bump and gather the upstream changes to describe:

```bash
OLD_VER=$(cat "/tmp/tend-old-ver")
NEW_VER=$(grep -hoE '^# Generated by tend [0-9]+\.[0-9]+\.[0-9]+' \
  .github/workflows/tend-*.yaml 2>/dev/null \
  | sed -E 's/^# Generated by tend //' | sort -u | head -1)

TITLE="chore: update tend workflows"
if [ -n "$OLD_VER" ] && [ -n "$NEW_VER" ] && [ "$OLD_VER" != "$NEW_VER" ]; then
  TITLE="chore: update tend workflows ($OLD_VER → $NEW_VER)"
  # The real "what changed": squash-merge subjects between the two release
  # tags. First line of each upstream commit; empty if the call fails, in
  # which case the body carries only the version line.
  gh api "repos/max-sixty/tend/compare/$OLD_VER...$NEW_VER" \
    --jq '.commits[].commit.message | split("\n")[0]' \
    > "/tmp/tend-upstream-commits.txt" 2>/dev/null || true
fi
printf '%s\n' "$TITLE" > "/tmp/tend-pr-title"
echo "OLD=$OLD_VER NEW=$NEW_VER  compare: https://github.com/max-sixty/tend/compare/$OLD_VER...$NEW_VER"
```

Compose the PR body with the Write tool at `/tmp/tend-update-body.md` — describe the upgrade, **don't paste a file list** (the diff is just mechanical action-ref bumps):

- Open by noting this is the automated nightly regeneration of tend's workflow files — phrase it per-run, or fold it into the version summary.
- **Version bumped**: add a `**tend version:** OLD → NEW` line, then a short **Notable changes** list — 3–5 bullets summarizing the entries in `/tmp/tend-upstream-commits.txt`. Rewrite each `(#NNN)` ref as `max-sixty/tend#NNN` — a bare `#NNN` auto-links to this repo's own issues, not tend's. Filter to **consumer-relevant** changes only — harness/action behavior, skill updates (review, ci-fix, triage, nightly, etc.), generator output that changes the adopter's workflow files, CI-monitoring guidance. **Exclude** pure mechanics (`chore: regenerate workflows`, `chore: release`, action-pin and lockfile bumps) and **tend-internal items** that affect only tend's own development or release (e.g. release-publishing workflow, marketing site, integration-test fixtures, internal refactors with no adopter-visible effect). Close with the compare link printed above. If the commits file is empty (the compare call failed), keep just the version line and the compare link.
- **No version bump** (same-version regen): one sentence on what the regen changed (a generator template tweak the committed workflows were lagging). No version line, no commit list.

Then ship it:

```bash
TITLE=$(cat "/tmp/tend-pr-title")
git add -A .github/workflows .config
# A fresh /tmp worktree has no git identity; without this the commit fails with
# `Author identity unknown` and an empty branch gets pushed. Idempotent — see
# "Configure git identity before the first commit" in /tend-ci-runner:running-in-ci.
BOT_LOGIN=$(gh api user --jq '.login'); BOT_ID=$(gh api user --jq '.id')
git config --global user.name "$BOT_LOGIN"
git config --global user.email "${BOT_ID}+${BOT_LOGIN}@users.noreply.github.com"
git commit -m "$TITLE"
git push -u origin tend/update-workflows
gh pr create --title "$TITLE" --body-file "/tmp/tend-update-body.md"
cd -
git worktree remove "/tmp/tend-update-workflows" --force
```

## Step 8: Fix findings

Before acting on findings, check for duplicates and existing work:

```bash
gh issue list --state open --json number,title
gh pr list --state open --json number,title,headRefName
```

The default action is a PR, not an issue. If there's a plausible fix, make it — explain uncertainty in the PR description.

For each finding:

1. **Create a PR** — branch, fix, run full test suite, commit, push, create PR, poll CI. **Every bug fix must include a regression test that would have failed before the fix.** If a test is not feasible (e.g., pure documentation changes), note why in the PR description. When uncertain about the approach, explain the trade-offs in the description.
2. **Create an issue only when there's no obvious fix** — design questions, problems needing maintainer input, or findings requiring investigation beyond what the survey can provide.

## Optional steps

Not run by default. Only run a step here when the project's `running-tend` skill explicitly enables it.

### Changelog maintenance

Keep the project's changelog up to date with recent changes. The `running-tend` skill specifies the changelog file and the branch to push to.

1. Find the changelog file. If it doesn't exist, skip — don't create one.
2. Check out the changelog branch. Create it from the default branch if it doesn't exist yet.
3. Merge the default branch into the changelog branch to stay current. If the merge conflicts, delete the branch, recreate it from the default branch, and start fresh.
4. Identify merged PRs and notable commits since the last entry in the changelog.
5. Draft entries matching the existing file's style and format.
6. Commit and push directly to the changelog branch — no PR needed, the branch is kept ready to merge for the next release.

## Step 9: Summary

Report: commits reviewed, files surveyed, findings, actions taken, assessment (clean / minor issues / needs attention).
