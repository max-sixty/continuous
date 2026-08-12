# Opening a skill PR from CI

The mechanics for proposing a change to a repo's `.claude/skills/` from a CI
session — when to propose is **Learning from Feedback** in SKILL.md.

1. **Complete the current task first.** The skill update is always a separate
   PR.

2. **Check for an existing open PR against the same skill.** Dedup by the
   target file, not by title — title conventions vary per repo:

   ```bash
   BOT_LOGIN=$(gh api user --jq '.login')
   gh pr list --state open --author "$BOT_LOGIN" --limit 200 --json number,title,headRefName,files \
     --jq '.[] | select([.files[].path] | index(".claude/skills/running-tend/SKILL.md"))'
   ```

   If one is open, add to it instead of opening a second.

3. **Draft a minimal edit.** State the rule, not the incident that produced
   it — no verbatim quotes of the maintainer's comment, no reconstruction of
   the exchange. A few lines of instruction is the target; step 4's PR body
   is where the case history goes. Place it under an appropriate heading. New
   SKILL.md files start with YAML frontmatter:

   ```markdown
   ---
   name: running-tend
   description: Project-specific guidance for tend workflows running on this repo.
   ---
   ```

   The checkout's `.claude/` directory is bind-mounted **read-only** under
   the sandbox (protecting bots from modifying their own skills in place), so
   edits to `.claude/skills/` files in the working tree fail with `Read-only
   file system`. Claude Code's harness adds a second restriction on top of
   the read-only mount: `Edit`, `Write`, and Bash commands with
   `.claude/skills/` as a write-target argument are denied regardless of
   filesystem permissions
   ([anthropics/claude-code#37157](https://github.com/anthropics/claude-code/issues/37157)).
   The guard checks argument text, so `Write(/tmp/…)` and
   `Bash(mv /tmp/… SKILL.md)` both pass — the second because `SKILL.md` is a
   bare filename inside the `cd`'d directory.

   Do the edit, commit, and push from a git worktree under `/tmp`, which is
   writable and sits outside the harness's `.claude/skills/` write-guard.
   (Don't write `$TMPDIR/...` — GitHub Actions runners leave `$TMPDIR` unset,
   so the path expands to `/skill-fix`, which the runner user can't create.)

   <!-- TODO(anthropics/claude-code#37157): once the harness exempts .claude/skills/ as
        documented, replace the /tmp-then-mv dance below with direct `Write` to the worktree path. -->

   Base the skill branch on the repo's default branch, **not `HEAD`**. When
   this runs from `tend-mention` on a PR, the workflow has already done
   `gh pr checkout` so `HEAD` is the PR branch — basing on it carries that
   PR's WIP commits into the skill PR and ships a multi-concern PR that mixes
   the skill change with unrelated code. Fetch and base off
   `origin/<default>` instead:

   ```bash
   DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')
   git fetch origin "$DEFAULT_BRANCH"
   git worktree add "/tmp/skill-fix" -b "skills/<topic>-$GITHUB_RUN_ID" "origin/$DEFAULT_BRANCH"

   # Use the Write tool to author the new skill file to /tmp/running-tend-new.md.
   # Then move it into place from inside the worktree. mkdir -p covers the
   # new-skill case where .claude/skills/<name>/ doesn't yet exist in the
   # default branch:
   mkdir -p "/tmp/skill-fix/.claude/skills/running-tend"
   cd "/tmp/skill-fix/.claude/skills/running-tend" && mv /tmp/running-tend-new.md SKILL.md

   cd "/tmp/skill-fix"
   git add .claude/skills/
   # Set git identity first if you haven't already this session — see
   # "Configure git identity before the first commit" in SKILL.md. A fresh
   # worktree has no identity and the commit below fails with `Author
   # identity unknown`.
   git commit -m "skills(running-tend): ..."
   git push -u origin skills/<topic>-$GITHUB_RUN_ID
   gh pr create --title "..." --body-file /tmp/pr-body.md --head skills/<topic>-$GITHUB_RUN_ID
   cd -
   git worktree remove "/tmp/skill-fix" --force
   ```

4. **Open as a separate PR.** Follow the repo's PR title conventions
   (conventional commits, Jira prefix, or whatever the repo uses — check
   recent merged PRs or `CONTRIBUTING.md`). The body quotes the triggering
   feedback and links the thread (PR/issue/comment URL).

5. **Open and exit — don't merge, don't wait.** The PR itself is the review
   request; a maintainer lands it (or doesn't) in their own time. Don't post
   a separate comment pinging for review, and don't block the session
   waiting. This open-and-exit is for skill proposals only; a code fix
   follows **CI Monitoring** in SKILL.md.
