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

Read every commit author first — it decides the path.

| Commit authors | Path |
| --- | --- |
| The owning bot alone | Rebuild, per the trigger table. |
| The owning bot plus this bot | Merge and push it yourself, per **Configured-bot PRs**. |
| Anyone else | Leave for manual resolution. |

| PR author | Required commit-author login | Rebuild trigger |
| --- | --- | --- |
| `app/dependabot` | `dependabot[bot]` | Comment `@dependabot recreate`. |
| `app/renovate` | `renovate[bot]` | In the PR body, check `<!-- rebase-check -->`. |

A rebuild overwrites the branch, so it fits only a branch this bot never
touched. `review` pushes fixes to dependency-bot PRs by design, and the owning
bot stops resolving conflicts on a branch that has been altered — leaving a
rebuild that would discard the fix as the only trigger, and the PR wedged at
its first conflict. That same commit is what makes the branch this bot's to
merge: resolve it under the exact head lease, as for a configured-bot PR. Never
force-push over a commit from anyone else.

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
still open at the original head and has no same-head deferral, create one
bot-authored comment that explains manual resolution is needed. Its final line
must be exactly:

```markdown
<!-- tend-conflict-deferred head=<head SHA> -->
```

Find prior deferrals through the paginated issue-comment API. After creating a
comment, re-read the head and comments. Delete only the new comment if the head
changed or an older same-head deferral won a concurrent race. Never edit another
head's deferral. The frequent poll skips the marked head; nightly retries it,
and a new head is eligible immediately.

Remove the temporary worktrees when all subagents finish.
