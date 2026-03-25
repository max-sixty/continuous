---
name: running-in-ci
description: Generic CI environment rules for GitHub Actions workflows. Use when operating in CI — covers security, CI monitoring, comment formatting, and investigating session logs from other runs.
metadata:
  internal: true
---

# Running in CI

## First Steps — Load Repo-Specific Guidance

Most repos have a project-specific overlay skill (typically `running-tend`)
with conventions the generic tend skills don't know — test commands, labels,
branch naming, survey scripts, codecov requirements. Check for one and load it
before doing anything else:

```bash
ls .claude/skills/
```

## Read Context

When triggered by a comment or issue, read the full context before responding.
The prompt provides a URL — extract the PR/issue number from it.

For PRs:

```bash
gh pr view <number> --json title,body,comments,reviews,state,statusCheckRollup
gh pr diff <number>
gh pr checks <number>
```

For issues:

```bash
gh issue view <number> --json title,body,comments,state
```

Read the triggering comment, the PR/issue description, the diff (for PRs), and
recent comments to understand the full conversation before taking action.

## Security

NEVER run commands that could expose secrets (`env`, `printenv`, `set`,
`export`, `cat`/`echo` on config files containing credentials). NEVER include
environment variables, API keys, tokens, or credentials in responses or
comments.

## PR Creation

When asked to create a PR, use `gh pr create` directly.

Before creating a branch or PR, check for existing work:

```bash
gh pr list --state open --json number,title,headRefName --jq '.[] | "#\(.number) [\(.headRefName)]: \(.title)"'
git branch -r --list 'origin/fix/*'
```

If an existing PR addresses the same problem, work on that PR instead.

## Pushing to PR Branches

Always use `git push` without specifying a remote — `gh pr checkout` configures
tracking to the correct remote, including for fork PRs. Specifying `origin`
explicitly can push to the wrong place.

If pushing fails (fork PR with edits disabled), fall back to posting code
snippets in a comment. Don't reference commit SHAs from temporary branches —
post code inline.

## CI Monitoring

After pushing, wait for CI before reporting completion.

**Use `run_in_background: true`** for the polling loop so it does not block the
session. When the background task completes you will be notified — check the
result and take any follow-up action (dismiss approval, post analysis) at that
point.

```bash
# Run with Bash tool's run_in_background: true
# Filter out the current workflow ($GITHUB_WORKFLOW) — it will always show as
# "pending" since it IS the running job. Watching yourself deadlocks.
for i in $(seq 1 10); do
  sleep 60
  if ! gh pr checks <number> --required 2>&1 | grep -v "$GITHUB_WORKFLOW" | grep -q 'pending\|queued\|in_progress'; then
    gh pr checks <number> --required
    exit 0
  fi
done
echo "CI still running after 10 minutes"
exit 1
```

1. Poll `gh pr checks <number> --required` every 60 seconds until all required
   checks complete (up to ~10 minutes). Ignore non-required checks (benchmarks).
   **Filter out `$GITHUB_WORKFLOW`** — the current workflow's own check is always
   pending while polling and must be excluded to avoid a deadlock.
2. If a required check fails, diagnose with `gh run view <run-id> --log-failed`,
   fix, commit, push, repeat.
3. Report completion only after all required checks pass.

**NEVER use `gh run watch` or `gh pr checks --watch`** — both hang indefinitely
and consume the entire job timeout. Always poll with `gh pr checks` in a loop.

Before dismissing local test failures as "pre-existing", check main branch CI:

```bash
gh api "repos/{owner}/{repo}/actions/runs?branch=main&status=completed&per_page=3" \
  --jq '.workflow_runs[] | {conclusion, created_at: .created_at}'
```

If you cannot verify, say "I haven't confirmed whether these failures are
pre-existing."

## Replying to Comments

Reply in context rather than creating new top-level comments:

- **Inline review comments** (`#discussion_r`): To read a single review
  comment, use the comment ID **without** the PR number in the path:
  ```bash
  gh api repos/{owner}/{repo}/pulls/comments/{comment_id}
  ```
  To reply:
  ```bash
  cat > /tmp/reply.md << 'EOF'
  Your response here
  EOF
  gh api repos/{owner}/{repo}/pulls/{number}/comments/{comment_id}/replies \
    -F body=@/tmp/reply.md
  ```

- **Conversation comments** (`#issuecomment-`): Post a regular comment (GitHub
  doesn't support threading).

## Comment Formatting

Keep comments concise. Put supporting detail inside `<details>` tags — the
reader should get the gist without expanding. Don't collapse content that *is*
the answer (e.g., a requested analysis).

```
<details><summary>Detailed findings (6 files)</summary>

...details here...

</details>
```

Always use markdown links for files, issues, PRs, and docs. Prefer permalinks
(commit SHA URLs) over branch-based links for line references — line numbers
shift and `blob/main/...#L42` links go stale.

- **Files**: link to GitHub (`blob/main/...` for file-level,
  `blob/<sha>/...#L42` for lines)
- **Issues/PRs**: `#123` shorthand
- **External**: `[text](url)` format

Don't add job links or footers — `claude-code-action` adds these automatically.

## Shell Quoting

Shell expansion corrupts `$` and `!` in arguments (bash history expansion
mangles `!` in double-quoted strings). Always use a temp file for comment bodies
and shell-sensitive arguments:

```bash
# Comments — ALWAYS use a file
cat > /tmp/comment.md << 'EOF'
Fixed — the `format!` macro needed its arguments on separate lines.
EOF
gh pr comment 1286 -F /tmp/comment.md

# Review replies — ALWAYS use a file
cat > /tmp/reply.md << 'EOF'
Good catch! Updated to use `assert_eq!` instead.
EOF
gh api repos/{owner}/{repo}/pulls/{number}/comments/{id}/replies \
  -F body=@/tmp/reply.md

# GraphQL — write query to a file
cat > /tmp/query.graphql << 'GRAPHQL'
query($owner: String!, $repo: String!) { ... }
GRAPHQL
gh api graphql -F query=@/tmp/query.graphql -f owner="$OWNER"

# jq with ! — use a file
cat > /tmp/jq_filter << 'EOF'
select(.status != "COMPLETED")
EOF
gh api ... --jq "$(cat /tmp/jq_filter)"
```

Use `<< 'EOF'` (single-quoted delimiter) to prevent expansion. For file-based
bodies: `gh pr comment` uses `-F /tmp/file` (path directly), while `gh api`
uses `-F body=@/tmp/file` (field assignment with `@` prefix).

## Keeping PR Titles and Descriptions Current

When revising code after review feedback, update the title and description if
the approach changed:

```bash
gh api repos/{owner}/{repo}/pulls/{number} -X PATCH \
  -f title="new title" -F body=@/tmp/updated-body.md
```

## Atomic PRs

Split unrelated changes into separate PRs — one concern per PR. If one change
could be reverted without affecting the other, they belong in separate PRs.

## Investigating Other CI Runs

Load `/install-tend:debug-ci-session` for session log download, JSONL parsing
queries, and diagnostic workflow. The primary evidence for diagnosing bot
behavior is the session log artifact — not console output.

Review-response runs triggered by `pull_request_review` or
`pull_request_review_comment` events sometimes produce no artifact when the
session is very short.

## Grounded Analysis

CI runs are not interactive — every claim must be grounded in evidence. The user
can't ask follow-up questions; treat every response as your final answer.

Read logs, code, and API data before drawing conclusions. Show evidence: cite
log lines, file paths, commit SHAs. Trace causation — if two things co-occur,
find the mechanism rather than saying "this may be related." Never claim a
failure is "pre-existing" without checking main branch CI history. Distinguish
what you verified from what you inferred.

### User-facing comments require source evidence

Public comments — on issues, PRs, or in review threads — are permanent and
visible. A hallucinated detail (wrong syntax, invented API, nonexistent flag)
misleads users and erodes trust. **It is always better to take longer and
produce a correct response than to respond quickly with fabricated details.**

Before posting any specific claim — a configuration snippet, command syntax,
variable name, or API behavior — find the **source text** that confirms it.
Source text means documentation, help output, test expectations, or the code
that implements the public interface. Internal implementation code (struct
fields, variable names in Rust/Python/etc.) shows what exists internally but not
how it's exposed to users — read the docs or user-facing layer too.

<example>
<bad reason="Read Rust code showing a 'target' variable and invented $WT_TARGET">

Bad: Saw `extra_vars.push(("target", target_branch))` in Rust source →
posted a hook example using `$WT_TARGET` (an environment variable that doesn't
exist — hooks use `{{ target }}` Jinja templates).

</bad>
<good reason="Verified syntax against user-facing documentation before posting">

Good: Saw `("target", target_branch)` in Rust source → read `docs/hook.md` →
confirmed hooks use `{{ target }}` syntax → posted correct example.

</good>
</example>

When a project has user-facing documentation (a docs site, `--help` pages, a
wiki), link to it. A link to the relevant docs page is more useful than a
paraphrased explanation — and finding the link forces verifying the claim.

If you can't find source evidence for a specific detail, say so ("I'm not sure
of the exact syntax") rather than guessing. An honest gap is fixable; a
confident hallucination gets copy-pasted.

## Tone

Raise observations, don't assign work. Never create checklists or task lists
for the PR author.

## PR Review Comments

For review comments on specific lines (`[Comment on path:line]`), read that file
and examine the code at that line before answering.

When the GitHub API returns a `diff_hunk`, the reviewer's comment targets the
**last line** of that hunk. Use this to disambiguate when multiple candidates
exist nearby — match the reviewer's request against the specific anchored line,
not the surrounding region.
