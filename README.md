<h1><img src="assets/logo-512.png" alt="tend logo" width="50" align="absmiddle">Tend</h1>

> **Early development** — extracted from [worktrunk](https://github.com/max-sixty/worktrunk)'s CI automation. Expect breaking changes.

Claude-powered CI for GitHub repos. PR review, issue triage, @bot mentions,
CI fixes, nightly sweeps, dependency updates.

## How it works

Three sources of behavior:

### `tend init` — workflow files

`uvx tend init` reads `.config/tend.toml` and writes `tend-*.yaml` into
`.github/workflows/`. Each workflow handles everything GitHub needs before
Claude runs: triggers, conditions (skip drafts, prevent bot self-loops),
engagement verification, concurrency, permissions, checkout strategy, setup
steps, and event-specific prompts. All six are enabled by default.

| Workflow       | Trigger                              | Skill             |
| -------------- | ------------------------------------ | ----------------- |
| `tend-review`  | PR opened/updated, review submitted  | `review`          |
| `tend-mention` | @bot mentions, engaged conversations | — (prompt-driven) |
| `tend-triage`  | Issue opened                         | `triage`          |
| `tend-ci-fix`  | CI fails on default branch           | `ci-fix`          |
| `tend-nightly` | Daily schedule, manual dispatch      | `nightly`         |
| `tend-weekly`  | Weekly schedule, manual dispatch     | `weekly`          |

Each workflow ends with `uses: max-sixty/tend@v1`, handing off to the action.

### Action (`max-sixty/tend@v1`)

The composite action runs the same steps regardless of which workflow
triggered it: security preflight (branch protection), rate limit preflight
(burst and daily spike detection), bot identity resolution, then invokes
`claude-code-action` with plugins, model, and allowed tools. Uploads session
logs as build artifacts afterward.

### Skills (`tend-ci-runner` plugin)

Skills define what Claude does once running. The action loads the
`tend-ci-runner` plugin automatically; each workflow's prompt invokes the
corresponding skill (see table above). `running-in-ci` loads first in every
session — CI environment rules, security boundaries, comment formatting.

A separate `install-tend` plugin provides user-facing skills: `install-tend`
(interactive repo setup) and `debug-ci-session` (session log analysis).

## Quick start

The fastest way to set up tend is with the `install-tend` skill, which handles
config, workflows, bot account, secrets, branch protection, and collaborator
setup interactively:

```sh
/install-tend my-project-bot
```

See the [install-tend skill](plugins/install-tend/skills/install-tend/SKILL.md)
for the full step-by-step procedure. The rest of this README covers config
options.

### Plugin install

Install the `install-tend` plugin for interactive setup:

```bash
claude plugin marketplace add max-sixty/tend   # one-time: register the repo as a marketplace
claude plugin install install-tend
```

The CI skills (`tend` plugin) are loaded automatically by the composite action —
you don't need to install them locally.

## Config

Create `.config/tend.toml`:

```toml
bot_name = "my-project-bot"
```

Only overrides from defaults are needed.

### Protected branches

The default branch is always protected. To protect additional branches (e.g.,
release branches), list them explicitly:

```toml
protected_branches = ["v1", "v2"]
```

`tend check` verifies branch protection on all listed branches. `tend check
--fix` creates a single ruleset covering the default branch and all extra
branches.

### Secrets

Two repo secrets are required:

| Secret                    | Value                                                                                                                       |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `BOT_TOKEN`               | Bot account's PAT — classic with `repo` scope, or fine-grained with `contents:write`, `pull-requests:write`, `issues:write` |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token (via OAuth PKCE flow, not an API key)                                                               |

Override secret names if yours differ:

```toml
[secrets]
bot_token = "MY_BOT_PAT"
claude_token = "MY_CLAUDE_TOKEN"
```

`tend check` flags any repo-level secret not in an explicit allowlist (the bot
tokens above are always allowed). Repos with additional legitimate repo-level
secrets — coverage tokens, linter keys — must list them:

```toml
[secrets]
allowed = ["CODECOV_TOKEN"]
```

Release secrets (registry tokens, signing keys) should never be repo-level.
Store them in a protected GitHub Environment instead — see
`docs/security-model.md`.

### Setup steps

Build tools, caches, and environment variables run before Claude in every
workflow:

```toml
setup = [
  {uses = "./.github/actions/my-setup"},
  {run = "echo CARGO_TERM_COLOR=always >> $GITHUB_ENV"},
]
```

For actions that need `with:` parameters, use `{raw = "..."}` — a multiline
string of GitHub Actions YAML injected verbatim:

```toml
setup = [
  {uses = "cargo-bins/cargo-binstall@main"},
  {run = "cargo binstall cargo-insta --no-confirm"},
  {raw = """
- uses: Swatinem/rust-cache@v2
  with:
    save-if: false
"""},
]
```

Each entry is `{uses = "..."}`, `{run = "..."}`, or `{raw = "..."}`. For very
complex setups, a local composite action
(`.github/actions/tend-setup/action.yaml`) referenced via `uses` is an
alternative.

### Workflow overrides

```toml
[workflows.ci-fix]
watched_workflows = ["ci", "build"]   # which workflows trigger ci-fix

[workflows.nightly]
cron = "0 8 * * *"                    # override default schedule
prompt = "/my-custom-nightly"         # override the default prompt

[workflows.weekly]
enabled = false                       # disable a workflow entirely
```

## Project context

Tend reads `CLAUDE.md` like any other Claude session. Put build/test/lint
commands and project conventions there.

For tend-specific guidance that doesn't belong in CLAUDE.md, add a skill overlay
at `.claude/skills/running-tend/SKILL.md`. The main use is recording which CI
workflow names tend-ci-fix watches. Other project-specific conventions (PR title
format, label policies) can be added if relevant.

## Migrating from claude-code-action

Repos using `anthropics/claude-code-action` should delete that workflow — tend
replaces it. Update team members to @-mention the bot account instead of
`@claude`. Verify no other workflows reference `anthropics/claude-code-action`.

## Limitations

### Inline review comments on fork PRs

GitHub has no event type that provides secret access for inline code review
comments on fork PRs. The `pull_request_review_comment` event fires, but
[secrets are unavailable for workflows triggered from forks][gh-secrets-forks].
Unlike `pull_request` (which has `pull_request_target` as a secrets-safe
equivalent), there is no `pull_request_review_comment_target` — GitHub has
[no plans to add one][gh-discussion-55940].

In practice this means `tend-mention` cannot respond to inline review comments
on fork PRs. Conversation-tab comments work fine — the `issue_comment` event
always runs in the base repository context with full secret access.

**Workaround:** comment on the conversation tab instead of inline when
interacting with the bot on a fork PR.

[gh-secrets-forks]: https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions#using-secrets-in-a-workflow
[gh-discussion-55940]: https://github.com/orgs/community/discussions/55940

## Security

See [docs/security-model.md](docs/security-model.md).

## License

MIT
