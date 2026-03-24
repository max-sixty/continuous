# Tend

> **Early development** — extracted from [worktrunk](https://github.com/max-sixty/worktrunk)'s CI automation. Expect breaking changes.

Claude-powered CI for GitHub repos. PR review, issue triage, @bot mentions,
CI fixes, nightly sweeps, dependency updates.

## How it works

Four pieces:

1. **Plugin** (`tend`) — Claude Code plugin providing CI skills (review,
   triage, ci-fix, nightly, renovate, etc.). Install from the marketplace or
   directly from the repo.

2. **Composite action** (`max-sixty/tend@v1`) — resolves bot ID at
   runtime, runs Claude Code, uploads session logs. The stable interface.

3. **Generator** (`uvx tend init`) — stamps out workflow files into
   `.github/workflows/`. Handles triggers, conditions, engagement verification,
   checkout. Idempotent — always overwrites from config.

4. **Config** (`.config/tend.toml`) — bot identity, secret names, project
   setup steps. Only overrides from defaults are needed.

## Quick start

### 1. Create a bot account

Create a GitHub user account for the bot (e.g., `my-project-bot`). Generate a
classic PAT with scopes: `repo` (or fine-grained with `contents:write`,
`pull-requests:write`, `issues:write`).

### 2. Add repo secrets

| Secret | Value |
|--------|-------|
| `BOT_TOKEN` | The bot account's PAT |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token from [console.anthropic.com](https://console.anthropic.com) |

### 3. Protect the default branch

The bot must not be able to merge PRs — this is the primary security boundary.
Use a **ruleset** ("Restrict updates" on the default branch, only admins
bypass) or **branch protection** (require reviews, don't exempt the bot).
See [docs/security-model.md](docs/security-model.md) for details.

### 4. Add config

Create `.config/tend.toml`:

```toml
bot_name = "my-project-bot"
```

This generates all six workflows using default secret names (`BOT_TOKEN`,
`CLAUDE_CODE_OAUTH_TOKEN`). Override secret names if yours differ:

```toml
bot_name = "my-project-bot"

[secrets]
bot_token = "MY_BOT_PAT"
claude_token = "MY_CLAUDE_TOKEN"
```

### 5. Install the plugin

Install the `tend` Claude Code plugin so the CI skills are available:

```bash
claude plugin add max-sixty/tend
```

### 6. Generate and commit

```bash
uvx tend init
uvx tend check          # verify branch protection, secrets, bot access
git add .github/workflows/tend-*.yaml .config/tend.toml
git commit -m "Add tend workflows"
git push
```

### 7. Add project context (recommended)

Without project-specific guidance, Claude uses only the generic CI skills. For
better results, add a `.claude/CLAUDE.md` with build commands, test commands,
and project conventions. For detailed per-workflow guidance, add a skill overlay
(see [Customization](#customization)).

## Customization

### Project setup steps

Build tools, caches, and environment variables run before Claude in every
workflow. Define them in config:

```toml
setup = [
  {uses = "./.github/actions/my-setup"},
  {run = "echo CARGO_TERM_COLOR=always >> $GITHUB_ENV"},
]
```

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

### Project-specific skills

The generic `tend-*` skills handle CI patterns. Project-specific behavior
(test commands, review criteria, labels) goes in a skill overlay in the
adopter's repo — e.g., `.claude/skills/running-tend/SKILL.md`. This skill
can reference the generic skills and add project conventions.

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
| `tend-renovate` | Weekly schedule, manual dispatch |

## Architecture

```
tend/
├── .claude-plugin/
│   └── plugin.json   # Plugin manifest
├── skills/           # CI skills (distributed via plugin)
├── action.yaml       # Composite action (the interface)
├── scripts/          # Helper scripts (survey, run listing)
├── generator/        # Python package (uvx tend)
└── docs/
    └── security-model.md
```

## Security

See [docs/security-model.md](docs/security-model.md).

## License

MIT
