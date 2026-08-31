---
name: resolve-conflicts
description: Resolves merge conflicts on configured-bot PRs and, when requested, upstream dependency-bot PRs.
metadata:
  internal: true
---

# Resolve Conflicts

Load `/tend-ci-runner:running-in-ci` first.

Resolve configured-bot PRs. Include Dependabot and Renovate only when the
calling skill requests dependency bots.

## Find conflicts

List up to 100 open PRs per author, including the head and base names and SHAs.
Test each PR locally against its own base; GitHub's `mergeable` field can remain
`UNKNOWN` while it computes.

```bash
git fetch --quiet --force origin \
  "refs/heads/<base>:refs/tend/base/<number>" \
  "refs/pull/<number>/head:refs/tend/pr/<number>"
git merge-tree --write-tree \
  "refs/tend/base/<number>" "refs/tend/pr/<number>" >/dev/null
```

Treat a failed query or fetch as unverified, not clean. Remove any prior
conflict-deferral comment from a configured-bot PR that test-merges clean.
When called from `notifications`, use every issue-comment page and skip a PR
whose bot-authored marker names the live `headRefOid`. Nightly ignores the
marker and retries.

When the caller requests dependency bots, include `app/dependabot` and
`app/renovate`.

## Dependency-bot PRs

Before triggering a rebuild, confirm every commit author is the owning bot.
Human commits make a force-pushing rebuild unsafe.

| PR author | Required commit-author login | Trigger |
| --- | --- | --- |
| `app/dependabot` | `dependabot[bot]` | Comment `@dependabot recreate`. |
| `app/renovate` | `renovate[bot]` | In the PR body, check `<!-- rebase-check -->`. |

Leave mixed-author branches for manual resolution. Never push to a dependency
bot's branch.

## Configured-bot PRs

Set the global git identity from `gh api user`, then dispatch one subagent per
conflicted PR. Give each subagent an isolated `/tmp/pr-<number>` worktree.

For each PR:

1. Read and retain `headRefOid`, `headRefName`, `headRepository`, `baseRefName`,
   `baseRefOid`, and `state`. Stop unless the PR is open and its head is in this
   repository. Check out that exact head.
2. Fetch and merge the recorded base. Resolve the conflicts, stage them, and
   commit with `git commit --no-edit`.
3. Immediately before pushing, read those live fields again. If any changed,
   discard the local merge and restart. Verify the retained head is an ancestor
   of `HEAD`, then run `git push
   --force-with-lease="refs/heads/<headRefName>:<headRefOid>" origin
   "HEAD:refs/heads/<headRefName>"`. The exact lease is the final head guard.
4. Fetch the live base again and test the pushed head with `git merge-tree`.
   If it conflicts, merge the new base and repeat. Once clean, remove the bot's
   conflict-deferral comment and monitor CI per **CI Monitoring** in
   `/tend-ci-runner:running-in-ci`.

If resolution is too complex, abort the merge and re-read the PR. When it is
still open at the original head, create or update one bot-authored comment that
explains manual resolution is needed and ends with:

```markdown
<!-- tend-conflict-deferred head=<head SHA> -->
```

Find prior deferral comments through the paginated issue-comment API. Update a
comment only when it already names this head; otherwise create one. After the
write, re-read the head and comments. If the head changed, delete only the stale
comment. Otherwise keep the oldest current-head comment and remove every other
deferral comment. The frequent poll skips the marked head; nightly retries it,
and a new head is eligible immediately.

Remove the temporary worktrees when all subagents finish.
