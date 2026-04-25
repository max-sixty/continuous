---
name: running-in-ci
description: Generic CI environment rules for GitHub Actions workflows. Use when operating in CI — covers security, CI monitoring, comment formatting, and investigating session logs from other runs.
metadata:
  internal: true
---

# Running in CI

## First Steps — Load Repo-Specific Guidance

Tend's bundled skills provide defaults; the consuming repo's `running-tend` skill overlays
them. **Where the two conflict, the repo wins** — repo guidance takes precedence over bundled
guidance across every skill, not just this one.

If a `running-tend` skill is listed in your available skills, load it with the Skill tool
before doing anything else. It typically carries PR title conventions, label policies, custom
workflows to watch, and other repo-specific context.

Repo-local skills are invoked by their unprefixed name — `Skill: running-tend`, not
`Skill: tend-ci-runner:running-tend` (that prefix is reserved for this plugin's own skills, and
trying it returns `Unknown skill`).

Repo-local skills must have YAML frontmatter (`name` + `description`) to be auto-discovered.

If you are going to propose a code fix for a bug, load `/tend-ci-runner:triage` first — it
contains reproduction and testing gates that apply to all fix attempts, not just initial triage.

## Conduct

Follow the project's code of conduct. Avoid causing disruption — unnecessary comments, bulk
operations, unsolicited housekeeping.

### Helping vs. directing

Anyone can ask for help with a problem they raise: investigating a bug, answering a question,
creating an issue or PR to address it. These are proposals — a maintainer still decides what to
merge or act on.

Directing the bot to affect someone else's work — closing, reopening, or locking issues/PRs,
dismissing reviews, reverting commits, applying or removing labels, pushing commits to a PR
owned by another author — requires Maintainer-tier access. Before complying, check the
requester's `author_association`:

@author-association.md

For Maintainer-tier requesters, proceed. For anyone else, briefly explain that a maintainer
needs to make that call.

The test: "Am I helping this person with something they raised, or following a directive that
affects someone else's work?"

This follows the repo > bundled rule from First Steps. If a repo's `running-tend` skill
explicitly authorizes an action (e.g., closing duplicate issues during triage), follow the
repo-specific instruction.

## Read Context

When triggered by a comment or issue, read the full context before responding. The prompt provides
a URL — extract the PR/issue number from it.

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

Read the triggering comment, the PR/issue description, the diff (for PRs), and recent comments to
understand the full conversation before taking action.

## Restrictions

- **Secrets**: Never run commands that expose secrets (`env`, `printenv`, `set`, `export`,
  `cat`/`echo` on credential files). Never include tokens or credentials in responses or comments.
- **Merging**: Never merge PRs or enable auto-merge (`gh pr merge`, `gh pr merge --auto`). PRs are
  proposals — a maintainer decides when to merge.
- **Scope**: Do not create issues, PRs, or comments in repositories outside the organization, unless
  the target repo explicitly welcomes AI-created issues (e.g., in its CONTRIBUTING guide).
- **Hanging commands**: Never use `gh run watch` or `gh pr checks --watch` — both hang indefinitely.
  Poll with `gh pr checks` in a loop instead.

## PR Creation

When asked to create a PR, use `gh pr create` directly.

Before creating a branch or PR, check for existing work:

```bash
gh pr list --state open --json number,title,headRefName --jq '.[] | "#\(.number) [\(.headRefName)]: \(.title)"'
git branch -r --list 'origin/fix/*'
```

If an existing PR addresses the same problem, work on that PR instead.

### Dedup recheck immediately before `gh pr create`

A separate mention on a different issue/PR can trigger a concurrent run asking for the same fix.
Those runs are not serialized — each has its own concurrency group — so both may read an empty
`gh pr list` at session start and then each open their own PR minutes later, producing
near-duplicates. Re-run the check **as the last step before `gh pr create`**:

```bash
BOT_LOGIN=$(gh api user --jq '.login')
gh pr list --state open --author "$BOT_LOGIN" --json number,title,headRefName,createdAt
```

Compare by title keywords **and** the files the new PR would modify — two concurrent fixes for
the same bug typically pick different branch names, so a branch-name match is not sufficient.
If a sibling bot PR overlaps in scope, **do not create**: post a comment on the triggering
thread linking the existing PR and exit.

## Pushing to PR Branches

Always use `git push` without specifying a remote — `gh pr checkout` configures tracking to the
correct remote, including for fork PRs. Specifying `origin` explicitly can push to the wrong place.

If pushing fails (fork PR with edits disabled), fall back to posting code snippets in a comment.
Don't reference commit SHAs from temporary branches — post code inline.

## Merging Upstream into PR Branches

When asked to merge the default branch into a PR branch:

1. **Never use `--allow-unrelated-histories`.** If `git merge` fails because git can't find a
   merge base, the checkout is broken — investigate rather than forcing the merge.
   `--allow-unrelated-histories` treats both sides as disconnected, creating add/add conflicts in
   every file.

2. **Handle untracked file conflicts properly.** If `git merge origin/main` fails because
   untracked files would be overwritten by tracked files, stash them first — don't delete them:
   ```bash
   git stash --include-untracked
   git merge origin/main
   git stash pop
   ```

3. **Verify merge base exists** before merging:
   ```bash
   git merge-base origin/main HEAD
   ```
   If this fails, the branch history is disconnected. Re-checkout the PR with full history
   (`fetch-depth: 0`) before retrying.

## CI Monitoring

After pushing, wait for CI before reporting completion.

**Use `run_in_background: true`** for the polling loop so it does not block the session. When the
background task completes you will be notified — check the result and take any follow-up action
(dismiss approval, post analysis) at that point.

```bash
# Run with Bash tool's run_in_background: true.
#
# Poll statusCheckRollup — every check-run + status context on the commit.
# Exit when all non-own items are terminal.
#
# Why rollup, not `gh pr checks --required`:
# `--required` only returns required contexts that are ALREADY registered on
# the commit. An `if: always()` omnibus with a long `needs:` list (e.g.
# PRQL's `check-ok-to-merge`) only registers once every dependency has
# reached terminal. With `--required`, the loop would see only fast required
# contexts (e.g. `pre-commit.ci - pr`), exit green, and miss the matrix
# entirely. The rollup shows matrix jobs as IN_PROGRESS while they run, so
# we correctly wait for them, then for the omnibus once it registers.
# See https://github.com/max-sixty/tend/issues/305.
#
# The 30s grace re-check handles actual registration lag: when the matrix's
# last `needs:` job finishes, the omnibus check-run registers within a
# second or two. A poll in that narrow window might see PENDING=0; the
# grace re-check catches the newly-IN_PROGRESS omnibus. The 23-min gap
# described in #305 is NOT registration lag — that was the matrix running,
# during which matrix jobs are visibly IN_PROGRESS in the rollup.
#
# Filter out the current run ($GITHUB_RUN_ID) — its own CheckRun is
# IN_PROGRESS for the whole loop. Match on the run URL, not the check name:
# `gh pr checks` shows the job name (e.g. "review"), which does not match
# $GITHUB_WORKFLOW ("tend-review").
#
# Don't use mergeStateStatus as an exit signal. BLOCKED is a catch-all:
# required checks pending, branch out of date (`type: update` rulesets),
# required reviews missing, or our own check still running — all produce
# BLOCKED, indistinguishable without admin scope on branch protection.
pending() {
  gh pr view <number> --json statusCheckRollup \
    | jq --arg own "/runs/$GITHUB_RUN_ID/" '
      [.statusCheckRollup[]
       | select((.detailsUrl // .targetUrl // "") | test($own) | not)
       | (.status // .state)
       | select(. == "IN_PROGRESS" or . == "QUEUED" or . == "PENDING" or . == "WAITING" or . == "REQUESTED" or . == "EXPECTED")
      ] | length'
}
for i in $(seq 1 15); do
  sleep 60
  [ "$(pending)" -gt 0 ] && continue
  sleep 30
  [ "$(pending)" -eq 0 ] || continue
  gh pr checks <number>
  exit 0
done
echo "CI still running after 15 minutes"
exit 1
```

1. Poll every 60 seconds (up to ~15 minutes) until all non-own check-runs on the commit are
   terminal. **Filter out the current run's URL (`/runs/$GITHUB_RUN_ID/`)** — the current
   workflow's own check is always pending while polling and must be excluded to avoid a
   deadlock. The 30s grace re-check catches late-registering omnibus checks.
2. If a required check fails, diagnose with `gh run view <run-id> --log-failed`, fix, commit,
   push, repeat.
3. Report completion only after all required checks pass.

Before dismissing local test failures as "pre-existing", check main branch CI:

```bash
gh api "repos/{owner}/{repo}/actions/runs?branch=main&status=completed&per_page=3" \
  --jq '.workflow_runs[] | {conclusion, created_at: .created_at}'
```

If you cannot verify, say "I haven't confirmed whether these failures are pre-existing."

## Replying to Comments

Reply in context rather than creating new top-level comments:

- **Inline review comments** (`#discussion_r`): To read a single review comment, use the comment
  ID **without** the PR number in the path:
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

- **Review events with inline comments** (review ID in prompt): A review may include inline
  comments. Fetch them by review ID and reply to each individually:
  ```bash
  gh api repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/comments \
    --jq '.[] | {id: .id, path: .path, body: .body}'
  ```
  Reply to each comment using the inline review comment reply endpoint above.

- **Conversation comments** (`#issuecomment-`): Post a regular comment (GitHub doesn't support
  threading).

## Multi-way Conversations

Before responding, check how many distinct other participants are in the conversation.

- **Two-party** (you and one other participant): respond normally.
- **Multi-way** (multiple other participants): apply a stricter bar — only respond with concrete new information no one else provided: a code fix, reproduction, or specific technical detail.

Do not:
- Restate, agree with, or summarize what another participant just said
- Post "makes sense" or "good point" agreement comments
- Echo a user's findings back to them ("Good find!", "That's the smoking gun!")

A comment that responds to concerns you raised in a review is directed at you — briefly
acknowledge resolution or explain why concerns remain.

If a maintainer has already addressed the point, exit silently unless you can add something they missed.

## Self-conversation Guard

If you are responding to your own prior comment or review (not a human's reply to it), only
respond if there is a distinct role boundary (e.g., you are the reviewer on your own PR and need
to address review feedback). If there is no such role distinction, exit silently to avoid
self-conversation loops.

## Recheck Before Posting

**Before posting any comment or review**, re-fetch the current conversation state. Other
participants may comment while you work — even in short sessions, context can change between when
you read a thread and when you reply:

```bash
# For issues
gh issue view <number> --json comments --jq '.comments | length'

# For PRs (comments + reviews)
gh pr view <number> --json comments,reviews \
  --jq '{comments: (.comments | length), reviews: (.reviews | length)}'
```

Compare with the count you saw when you first read the context. If new comments or reviews
appeared:

1. **Read the new comments** to understand what changed.
2. **Adjust or skip your response.** If someone already answered, don't repeat them. If the author
   resolved the issue, acknowledge that instead of posting a stale analysis. If new information
   contradicts your findings, update your response before posting.
3. **If your response is now entirely redundant, don't post it.**

### Dedup check for inline review comment replies

A single PR review can fire both `pull_request_review` and `pull_request_review_comment` events,
triggering separate workflow runs (serialized by the concurrency group, not truly concurrent).
Before replying to an inline review comment, check whether the bot already replied:

```bash
BOT_LOGIN=$(gh api user --jq '.login')
EXISTING=$(gh api "repos/{owner}/{repo}/pulls/{number}/comments?per_page=100" \
  --jq "[.[] | select(.in_reply_to_id == {comment_id} and .user.login == \"$BOT_LOGIN\")] | length")
```

If `EXISTING` is greater than 0, **do not post** — another run already handled this comment. Exit
silently.

## Comment Formatting

**Line wrapping:** GitHub renders newlines literally in issue bodies, PR descriptions, and
comments — a line break in the source becomes a `<br>` in the output. Write each paragraph as a
single long line and let the browser reflow. Hard wraps at 72–80 chars create awkward mid-sentence
breaks on GitHub. (This applies to GitHub-rendered content only, not to skill files or code.)

This applies to every delivery path — heredoc, `--body "…"`, and `--body-file`. The Write tool
does not reflow paragraphs, so authoring to a file is not a fix on its own: the wrapping you
type is the wrapping that ships.

<example>
<bad reason="Paragraph hard-wrapped at ~72 chars; GitHub renders each newline as `<br>`, producing mid-sentence breaks">

```
Extend the bang-escape workaround in `running-in-ci` to cover PR and issue
titles. PR #318 restored the warning for comment bodies (via `--body-file`);
titles are still uncovered because `gh pr create` has no `--title-file` flag.
```

</bad>
<good reason="Paragraph is one long line; GitHub reflows it to the reader's window width">

```
Extend the bang-escape workaround in `running-in-ci` to cover PR and issue titles. PR #318 restored the warning for comment bodies (via `--body-file`); titles are still uncovered because `gh pr create` has no `--title-file` flag.
```

</good>
</example>

Code blocks, bullet lists, and tables keep their newlines as-is — only prose paragraphs need to
be unwrapped.

Keep comments concise. Put supporting detail inside `<details>` tags — the reader should get the
gist without expanding. Don't collapse content that *is* the answer (e.g., a requested analysis).

```
<details><summary>Detailed findings (6 files)</summary>

...details here...

</details>
```

Always use markdown links for files, issues, PRs, and docs. **Any link containing `#L` must
use a commit SHA, never `blob/main/...#L42`** — line numbers shift silently, so the link stays
valid but starts pointing at different code than the comment describes. Get the SHA with
`git rev-parse HEAD` before composing the link.

**GitHub URLs — always embed `$GITHUB_REPOSITORY`.** Construct links as
`https://github.com/${GITHUB_REPOSITORY}/...`; never hand-type the owner. The model reliably
guesses wrong — past comments have shipped with `anthropics/worktrunk` and `worktrunk/worktrunk`
on a repo actually owned by `max-sixty`. Before posting a comment, scan it for `github.com/` and
confirm every owner matches `$GITHUB_REPOSITORY`. **Also check that the variable actually
expanded** — if you see a literal `${GITHUB_REPOSITORY}` in the rendered comment, you used a
single-quoted heredoc (`<< 'EOF'`) which disables expansion. Rewrite using an unquoted `<<EOF`
(so `${GITHUB_REPOSITORY}` interpolates) or compose the body with the Write tool.

**If a Bash-tool command string contains a literal exclamation mark — comment body, jq
script, markdown heredoc, anything — use the Write tool. Heredocs and quoting do not save
you.** The Bash tool rewrites every exclamation mark to a literal backslash-bang before
bash parses the command, so a greeting like "Thanks for the suggestion!" renders as "Thanks
for the suggestion\!" in the posted comment. Quoting and heredoc form don't matter:
`<< 'EOF'`, `<<EOF`, plain single-quoted, and double-quoted arguments all lose the
character. Use the Write tool for any comment body containing an exclamation mark, then pass
the file to
`gh ... --body-file`. The same trap applies to `jq` and `--jq` filters with `!=` — either
avoid the `!=` operator (rephrase as `== "x" | not`), filter client-side after fetching, or
load the jq script from a file written with the Write tool via `jq -f`.

`gh pr create` / `gh pr edit` / `gh issue create` / `gh issue edit` have no
`--title-file` flag, so a title containing an exclamation mark (e.g. the
conventional-commits breaking-change marker in a title like "feat(hooks)!:
rename …") hits the same rewrite and ships a visibly corrupted title. Write the
title with the Write tool and pass it via command substitution — `$(cat …)` is
evaluated by bash after the preprocessor's string scan, so the exclamation mark
stays literal:

```bash
gh pr create --title "$(cat /tmp/pr-title.txt)" --body-file /tmp/pr-body.md ...
```

- **File-level link (no `#L` anchor)**: `blob/main/src/foo.rs` is fine
- **Line reference**: `blob/<sha>/src/foo.rs#L42` — commit SHA required, never `blob/main/...#L42`
- **Issues/PRs**: `#123` shorthand
- **External**: `[text](url)` format

Don't add job links or footers — `claude-code-action` adds these automatically.

## Keeping PR Titles and Descriptions Current

When revising code after review feedback, update the title and description if the approach changed:

```bash
gh api repos/{owner}/{repo}/pulls/{number} -X PATCH \
  -f title="new title" -F body=@/tmp/updated-body.md
```

## Atomic PRs

Split unrelated changes into separate PRs — one concern per PR. If one change could be reverted
without affecting the other, they belong in separate PRs.

## Investigating Other CI Runs

Load `/install-tend:debug-ci-session` for session log download, JSONL parsing queries, and
diagnostic workflow. The primary evidence for diagnosing bot behavior is the session log
artifact — not console output.

Review-response runs triggered by `pull_request_review` or `pull_request_review_comment` events
sometimes produce no artifact when the session is very short.

## Grounded Analysis

CI runs are not interactive — every claim must be grounded in evidence. The user can't ask
follow-up questions; treat every response as your final answer.

Read logs, code, and API data before drawing conclusions. Show evidence: cite log lines, file
paths, commit SHAs. Trace causation — if two things co-occur, find the mechanism rather than
saying "this may be related." Never claim a failure is "pre-existing" without checking main branch
CI history. Distinguish what you verified from what you inferred.

### User-facing comments require source evidence

Public comments — on issues, PRs, or in review threads — are permanent and visible. A
hallucinated detail (wrong syntax, invented API, nonexistent flag) misleads users and erodes
trust. **It is always better to take longer and produce a correct response than to respond quickly
with fabricated details.**

Before posting any specific claim — a configuration snippet, command syntax, variable name, or API
behavior — find the **source text** that confirms it. Source text means documentation, help output,
test expectations, or the code that implements the public interface. Internal implementation code
(struct fields, variable names in Rust/Python/etc.) shows what exists internally but not how it's
exposed to users — read the docs or user-facing layer too.

<example>
<bad reason="Read Rust code showing a 'target' variable and invented $WT_TARGET">

Bad: Saw `extra_vars.push(("target", target_branch))` in Rust source → posted a hook example
using `$WT_TARGET` (an environment variable that doesn't exist — hooks use `{{ target }}` Jinja
templates).

</bad>
<good reason="Verified syntax against user-facing documentation before posting">

Good: Saw `("target", target_branch)` in Rust source → read `docs/hook.md` → confirmed hooks use
`{{ target }}` syntax → posted correct example.

</good>
</example>

For **behavioral claims** — "X happens when you run Y", "command Z works in scenario W" — reading
code is not sufficient. Code has conditional branches, early returns, and error paths that are easy
to miss when tracing mentally. Before asserting what a command does in a specific scenario, either
find a test that exercises that exact scenario or run the command yourself. If neither is feasible,
hedge: "Based on code reading, I believe X, but I haven't verified this end-to-end."

<example>
<bad reason="Traced one code path but missed a guard clause in a called function">

Bad: Read `CommandEnv::for_action("commit", config)` → saw it constructs an env → concluded
`wt step commit` works in a detached worktree. Missed that `for_action()` calls
`require_current_branch()`, which errors on detached HEAD.

</bad>
<good reason="Built and tested the actual behavior before claiming">

Good: Read `for_action()` → noticed it calls `require_current_branch()` → uncertain whether
detached HEAD hits that path → ran `cargo build && wt step commit` in a detached worktree →
confirmed the error → posted accurate answer.

</good>
</example>

When a project has user-facing documentation (a docs site, `--help` pages, a wiki), link to it. A
link to the relevant docs page is more useful than a paraphrased explanation — and finding the link
forces verifying the claim.

If you can't find source evidence for a specific detail, say so ("I'm not sure of the exact
syntax") rather than guessing. An honest gap is fixable; a confident hallucination gets
copy-pasted.

### Rewriting is authoring

Cross-posting, summarizing, or paraphrasing is not copying — any new content you add requires the
same source verification as a fresh comment. If you expand with a config section header, code
block, or usage example, verify each addition against the source.

<example>
<bad reason="Composed new TOML section header without verifying it">

Bad: Cross-posted a hook snippet, added an alias example with `[step]` as the section header
(inferred from the `wt step` command name). The actual config section is `[aliases]`.

</bad>
<good reason="Verified new content against docs before posting">

Good: Cross-posted a hook snippet, added an alias example → checked `dev/*.example.toml` to
confirm the section is `[aliases]` → posted with correct syntax.

</good>
</example>

## Learning from Feedback

When a maintainer corrects the bot's behavior during a run — points out a repo convention, a
repeated mistake, or a preference the bot should have known — propose a follow-up PR against
the consuming repo's `.claude/skills/running-tend/SKILL.md`. This turns one-off corrections
into durable guidance for future runs in *this* repo. The PR targets the consuming repo, not
tend; bundled tend defaults change through separate PRs against the tend repo.

### When to propose

Only when feedback is **generalizable** — it should apply to future runs, not just the current
task. Signals:

- Correction names a pattern (*"stop adding inline suggestions for formatting — the linter
  handles that"*), not a task detail (*"rename this variable"*)
- Feedback references a repo convention (*"we use conventional commits"*, *"PRs go to the
  `develop` branch, not `main`"*)
- The same correction has surfaced before, or would plausibly surface again

Do **not** propose when:

- The feedback is task-specific (a one-off rewording, a particular variable name)
- The lesson is already covered by a bundled tend skill — those update through PRs against the
  tend repo, not per-repo overlays
- Confidence that the feedback generalizes is low — ask for clarification instead
- The feedback comes from a non-maintainer — check `author_association` and skip the
  skill PR. Non-maintainers can raise preferences, but only a maintainer authorizes codifying
  them. If the pattern is worth capturing, note it in a reply and let a maintainer confirm.

### Bundled-skill defects: ask permission to file in tend

When the correction identifies a gap or bug in a **bundled** skill — the same root cause would
fire in every tend consumer — open an issue in the current repo asking for permission to file
the same issue in tend. On maintainer approval, open the tend issue.

Signals:

- The fix reads as generic guidance that would apply to any consumer.
- The behavior being corrected comes from bundled skill text.

Include in the permission request (and reuse verbatim in the tend issue once approved):

- Problem statement: what fires, in which bundled skill, under what conditions.
- Evidence: run links; cost/duration if relevant.
- Proposed fix with code snippets a maintainer would otherwise re-derive.

### How to propose

1. **Complete the current task first.** The skill update is always a separate PR.
2. **Check for an existing open PR against the same skill.** Dedup by the target file, not
   by title — title conventions vary per repo:
   ```bash
   BOT_LOGIN=$(gh api user --jq '.login')
   gh pr list --state open --author "$BOT_LOGIN" --json number,title,headRefName,files \
     --jq '.[] | select([.files[].path] | index(".claude/skills/running-tend/SKILL.md"))'
   ```
   If one is open, add to it instead of opening a second.
3. **Draft a minimal edit.** One short rule, in the maintainer's words where practical. Place
   it under an appropriate heading. Use the `Write` tool to author the full new contents of
   the skill file to `/tmp/running-tend-new.md`, then move it into place via Bash:
   <!-- TODO(anthropics/claude-code#37157): remove the cd-and-mv workaround once the
        harness exempts .claude/skills/ as documented — then `Write` directly to the
        target path. -->
   ```bash
   cd .claude && mkdir -p skills/running-tend && cd skills/running-tend \
     && mv /tmp/running-tend-new.md SKILL.md
   ```
   The `cd` is required. Claude Code's harness blocks `Edit`, `Write`, and Bash commands
   whose write-target argument is a path under `.claude/skills/`
   ([anthropics/claude-code#37157](https://github.com/anthropics/claude-code/issues/37157)).
   The guard checks the argument text — `Write(/tmp/…)` and `Bash(mv /tmp/… SKILL.md)`
   both pass because neither names the protected path.

   New SKILL.md files start with YAML frontmatter:
   ```markdown
   ---
   name: running-tend
   description: Project-specific guidance for tend workflows running on this repo.
   ---
   ```
4. **Open as a separate PR.** Follow the repo's PR title conventions (conventional commits,
   Jira prefix, or whatever the repo uses — check recent merged PRs or `CONTRIBUTING.md`).
   The body quotes the triggering feedback and links the thread (PR/issue/comment URL).
5. **Open and exit — don't merge, don't wait.** The PR itself is the review request; a
   maintainer lands it (or doesn't) in their own time. Don't post a separate comment pinging
   for review, and don't block the session waiting.

## Tone

Raise observations, don't assign work. Never create checklists or task lists for the PR author.

## PR Review Comments

For review comments on specific lines (`[Comment on path:line]`), read that file and examine the
code at that line before answering.

When the GitHub API returns a `diff_hunk`, the reviewer's comment targets the **last line** of that
hunk. Use this to disambiguate when multiple candidates exist nearby — match the reviewer's request
against the specific anchored line, not the surrounding region.
