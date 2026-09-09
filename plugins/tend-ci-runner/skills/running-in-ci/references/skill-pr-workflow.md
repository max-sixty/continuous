# Opening a skill PR from CI

Turning a maintainer's correction into durable guidance: whether it clears the
bar, whether it lands upstream in tend or in the consuming repo's
`.claude/skills/running-tend/SKILL.md`, and the mechanics of proposing it.

## Whether to propose

The feedback must be **generalizable** — it applies to future runs, not just
this task — and clear at least one bar:

- **Recurrence**: the same correction seen at least twice, or direct evidence
  the failure mode recurs. "Saw it once, wrote a rule" is below the bar.
- **Invisible failure mode**: the bad behavior wouldn't surface as a future CI
  failure (a cancelled or timed-out run whose work actually succeeded), so
  nothing would catch it next time.
- **Maintainer asked** for the rule to be codified, even after one occurrence.

Bundled tend defaults go through human review on the tend repo, which acts as
an implicit recurrence filter; per-repo overlays don't, so the bar lives here.

Signals pointing at a generalizable rule: the correction names a pattern
("stop adding inline suggestions for formatting — the linter handles that")
rather than a task detail, or references a repo convention ("we use
conventional commits", "PRs go to `develop`").

Don't propose when the feedback is task-specific, when confidence that it
generalizes is low (ask instead), or when it comes from a non-maintainer —
check `author_association`. Non-maintainers can raise preferences, but only a
maintainer authorizes codifying them; note the pattern in a reply and let one
confirm.

## Where it lands

Settle the destination before drafting a line, and settle it on one question:
**would every tend consumer want this rule?** If yes it belongs in tend's
bundled skills, and what the bundled text currently says doesn't change that:

- **Bundled text is wrong or unclear** — fix it upstream.
- **Bundled text is silent** — the same gap, usually a wider one, since
  nothing is enforcing the rule for any consumer. Silence is not evidence the
  rule is local; it is the commonest reason a maintainer had to correct
  generic behavior in the first place.
- **The rule is already merged upstream, but not in the pinned release** — the
  fix is a release, not a local copy. The lag is temporary and overlay text is
  permanent: once the release ships, every consumer that forked the rule
  carries a duplicate someone has to notice and delete, and until then the two
  copies drift.

The overlay is for what is true of one repo alone — its branch and landing
conventions, its test topology, its trackers and labels, its standing
exceptions. A rule that reads as generic guidance goes upstream even when
writing it locally would be quicker.

From a consumer repo, upstream means an issue on tend, filed per **Filing
issues** in `other-repos.md`. Running on tend itself, it means a PR here.

## Mechanics

For the overlay path, and for an upstream PR on tend itself — with the bundled
skill file as step 2's dedup target, and without step 3's read-only-mount
workaround, since bundled skills live under `plugins/` rather than
`.claude/skills/`. Filing upstream from a consumer repo follows
`other-repos.md` instead.

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

   # Author the new skill file at /tmp/running-tend-new.md.
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
   recent merged PRs or `CONTRIBUTING.md`). The body states the generalized
   behavior gap and the outcome the new guidance should produce, then links
   the triggering thread as evidence. Do not quote or reconstruct the exchange.

5. **Open and exit — don't merge, don't wait.** The PR itself is the review
   request; a maintainer lands it (or doesn't) in their own time. Don't post
   a separate comment pinging for review, and don't block the session
   waiting. This open-and-exit is for skill proposals only; a code fix
   follows **CI Monitoring** in SKILL.md.
