# Continuous

> **Early development** — extracted from [worktrunk](https://github.com/max-sixty/worktrunk)'s CI automation. Expect breaking changes.

Claude-powered CI for GitHub repos. PR review, issue triage, @bot mentions,
CI fixes, nightly sweeps, dependency updates.

## How it works

Three pieces:

1. **Composite action** (`max-sixty/continuous@v1`) — installs generic skills,
   runs Claude Code, uploads session logs. The stable interface.

2. **Generator** (`uvx continuous init`) — stamps out workflow files into the
   adopter's `.github/workflows/`. Handles triggers, conditions, engagement
   verification, checkout. Preserves project-specific setup on regeneration.

3. **Config** (`.config/continuous.toml`) — bot identity and secret names. The
   generator reads this to produce workflows.

## Quick start

1. Create a bot GitHub account with a PAT (`contents:write`,
   `pull-requests:write`, `issues:write`).

2. Add repo secrets: `BOT_TOKEN` (the PAT) and `CLAUDE_CODE_OAUTH_TOKEN`.

3. Set up merge protection — the bot must not be able to merge PRs.

4. Add `.config/continuous.toml`:

   ```toml
   bot_name = "my-bot"
   bot_id = "123456789"

   [secrets]
   bot_token = "BOT_TOKEN"
   claude_token = "CLAUDE_CODE_OAUTH_TOKEN"

   [workflows.review]
   [workflows.mention]
   [workflows.triage]
   [workflows.ci-fix]
   [workflows.nightly]
   [workflows.renovate]
   ```

5. Generate and commit:

   ```bash
   uvx continuous init
   git add .github/workflows/cd-*.yaml
   git commit -m "Add continuous workflows"
   ```

6. Add project setup (build tools, caches) between the marker comments in each
   generated workflow, then push.

## Updating

```bash
uvx continuous update
```

Regenerates generator-owned sections. Content between `# --- project setup ---`
markers is preserved.

## What's generated

| Workflow | Trigger |
|---|---|
| `cd-review` | PR opened/updated, review submitted |
| `cd-mention` | @bot mentions, engaged conversations |
| `cd-triage` | Issue opened |
| `cd-ci-fix` | CI fails on default branch |
| `cd-nightly` | Daily schedule |
| `cd-renovate` | Weekly schedule |

## Architecture

```
continuous/
├── action.yaml       # Composite action (the interface)
├── skills/           # Generic CI skills for Claude
├── scripts/          # Helper scripts (survey, run listing)
├── generator/        # Python package (uvx continuous)
└── docs/
    └── security-model.md
```

Project-specific behavior (test commands, review criteria, labels) stays in the
adopter's repo as skill overlays that reference the generic `cd-*` skills.

## Security

See [docs/security-model.md](docs/security-model.md).

## License

MIT
