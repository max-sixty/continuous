# Tend

> **Early development** — extracted from [worktrunk](https://github.com/max-sixty/worktrunk)'s CI automation. Expect breaking changes.

Claude-powered CI for GitHub repos. PR review, issue triage, @bot mentions,
CI fixes, nightly sweeps, dependency updates.

## How it works

Four pieces:

1. **Plugins** — two Claude Code plugins distributed from the same
   marketplace. `install-tend` is user-facing (sets up tend on a new repo).
   `tend` provides CI skills (review, triage, ci-fix, nightly, renovate, etc.)
   loaded by the composite action.

2. **Composite action** (`max-sixty/tend@v1`) — resolves bot ID at
   runtime, runs Claude Code, uploads session logs. The stable interface.

3. **Generator** (`uvx tend init`) — stamps out workflow files into
   `.github/workflows/`. Handles triggers, conditions, engagement verification,
   checkout. Idempotent — always overwrites from config.

4. **Config** (`.config/tend.toml`) — bot identity, secret names, project
   setup steps. Only overrides from defaults are needed.

## Quick start

The fastest way to set up tend is with the `install-tend` skill, which handles
config, workflows, bot account, secrets, branch protection, and collaborator
setup interactively:

```
/install-tend my-project-bot
```

See the [install-tend skill](plugins/install-tend/skills/install-tend/SKILL.md)
for the full step-by-step procedure. The rest of this README covers config
options and what gets generated.

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

Only overrides from defaults are needed. If the repo's default branch isn't
`main`:

```toml
bot_name = "my-project-bot"
default_branch = "master"
```

### Secrets

Two repo secrets are required:

| Secret | Value |
|--------|-------|
| `BOT_TOKEN` | Bot account's classic PAT (`repo` scope) |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token (via OAuth PKCE flow, not an API key) |

Override secret names if yours differ:

```toml
[secrets]
bot_token = "MY_BOT_PAT"
claude_token = "MY_CLAUDE_TOKEN"
```

### Setup steps

Build tools, caches, and environment variables run before Claude in every
workflow:

```toml
[setup]
uses = ["./.github/actions/my-setup"]
run = ["echo CARGO_TERM_COLOR=always >> $GITHUB_ENV"]
```

For actions that need `with:` parameters, use `raw` — a multiline string of
GitHub Actions YAML injected verbatim into the workflow steps:

```toml
[setup]
uses = ["cargo-bins/cargo-binstall@main"]
run = ["cargo binstall cargo-insta --no-confirm"]
raw = """
- uses: Swatinem/rust-cache@v2
  with:
    save-if: false
"""
```

`uses` and `run` entries are bare strings (no `with:` support). `raw` handles
everything else. For very complex setups, a local composite action
(`.github/actions/tend-setup/action.yaml`) referenced via `uses` is an
alternative.

### Workflow overrides

```toml
[workflows.ci-fix]
watched_workflows = ["ci", "build"]   # which workflows trigger ci-fix

[workflows.nightly]
cron = "0 8 * * *"                    # override default schedule
prompt = "/my-custom-nightly"         # override the default prompt

[workflows.renovate]
enabled = false                       # disable a workflow entirely
```

## What's generated

All six workflows are enabled by default. Disable individual workflows with
`enabled = false` in config.

| Workflow | Trigger |
|---|---|
| `tend-review` | PR opened/updated, review submitted |
| `tend-mention` | @bot mentions, engaged conversations |
| `tend-triage` | Issue opened |
| `tend-ci-fix` | CI fails on default branch |
| `tend-nightly` | Daily schedule, manual dispatch |
| `tend-renovate` | Weekly schedule, manual dispatch — handles Dependabot, Renovate, and labeled dependency PRs |

## Project context

Tend reads `CLAUDE.md` like any other Claude session. Put build/test/lint
commands and project conventions there.

For tend-specific guidance that doesn't belong in CLAUDE.md, add a skill overlay
at `.claude/skills/running-tend/SKILL.md`. This is for things only relevant to
CI: PR title conventions, which CI workflow names tend-ci-fix watches, automerge
rules, dependency management preferences. Don't duplicate CLAUDE.md content.

## Migrating from claude-code-action

Repos using `anthropics/claude-code-action` should delete that workflow — tend
replaces it. Update team members to @-mention the bot account instead of
`@claude`. Verify no other workflows reference `anthropics/claude-code-action`.

## Architecture

```
tend/
├── .claude-plugin/
│   └── marketplace.json   # Lists both plugins
├── plugins/
│   ├── install-tend/      # User-facing plugin (setup skill)
│   └── tend/              # CI plugin (review, triage, ci-fix, etc.)
├── action.yaml            # Composite action (the interface)
├── scripts/               # Helper scripts (survey, run listing)
├── generator/             # Python package (uvx tend)
└── docs/
    └── security-model.md
```

## Security

See [docs/security-model.md](docs/security-model.md).

## License

MIT
