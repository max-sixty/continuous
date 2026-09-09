## Skills (Codex-specific)

The `tend-ci-runner` and `install-tend` plugins are installed. The runner
skills (`review`, `code-review`, `triage`, `ci-fix`, `nightly`, `weekly`,
`notifications`, `review-runs`, `running-in-ci`, `review-reviewers`, and
`resolve-conflicts`) are invocable via `$<skill-name>` mentions in prompts.
The diagnostic `/install-tend:debug-tend-run` skill is available to runner
skills that inspect earlier Tend sessions.

**Read each tend skill in full.** When you open a `tend-ci-runner`
`SKILL.md`, read the entire file with `cat`. Do not read a prefix with
`sed -n '1,Np'` or `head`. These skills are short, and their trailing
sections carry load-bearing security, dedup, and CI-polling rules. A
prefix read silently drops those and produces wrong behavior. This
overrides any general "read only enough" guidance for tend skills.

ALWAYS read `$running-in-ci` first (in full) when handling any
workflow. It covers CI security rules, polling conventions, and
comment-formatting guidance. Other skills depend on it.

Repo-local skills live under `.claude/skills/<name>/SKILL.md` in the
adopter's repo (e.g. `running-tend`). The `running-in-ci` skill tells
you when to read them; read those in full too.

## Tooling

- `gh` is authenticated as the bot via `$GH_TOKEN`. Use it to post
  comments, open PRs, push commits.
- `uv` is on PATH for Python environments and one-shot tools.
- The bot's user ID is available via `gh api users/${BOT_NAME} --jq .id`
  if you need it for `author.id` comparisons.
