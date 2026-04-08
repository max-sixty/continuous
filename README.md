<h1><img src="assets/logo-512.png" alt="tend logo" width="50" align="absmiddle">Tend</h1>

Tend runs Claude Code as a GitHub Actions bot. It reviews pull requests,
triages issues, fixes CI failures, and sweeps the repo on a schedule. A
merge restriction prevents the bot from merging unreviewed code.

<!-- TODO: add screenshot of a review comment, triage response, or CI fix PR -->

## Quick start

```sh
claude plugin marketplace add max-sixty/tend
claude plugin install install-tend@tend
/install-tend my-project-bot
```

This handles config, workflow generation, bot account, secrets, and branch
protection. Only the setup plugin needs manual installation — CI skills load
automatically at runtime.

The [install-tend skill](plugins/install-tend/skills/install-tend/SKILL.md)
documents each step.

## Workflows

| Workflow          | Trigger                     | What happens                                                                                                               |
| ----------------- | --------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **review**        | PR opened/updated           | Reviews for correctness and duplication. Traces error paths. Monitors CI. Pushes mechanical fixes to bot-authored PRs.     |
| **mention**       | @bot mention, review        | Responds to requests in PR and issue conversations.                                                                        |
| **triage**        | Issue opened                | Classifies the issue, checks for duplicates, reproduces bugs, attempts conservative fixes.                                 |
| **ci-fix**        | CI fails on default branch  | Reads failure logs, identifies root cause, searches for the same pattern elsewhere, opens a fix PR.                        |
| **nightly**       | Daily / manual dispatch     | Resolves conflicts on open PRs, reviews recent commits, surveys ~10 files for bugs and stale docs, closes resolved issues. |
| **weekly**        | Weekly / manual dispatch    | Regenerates tend workflow files, reviews dependency PRs, auto-merges safe patch and minor updates.                         |
| **notifications** | Every 15 minutes            | Polls GitHub notifications, responds to unhandled mentions, marks handled threads as read.                                 |

All are enabled by default except **ci-fix**, which requires
`watched_workflows` to be configured. Any can be disabled:

```toml
[workflows.weekly]
enabled = false
```

## How it works

`uvx tend@latest init` reads `.config/tend.toml` and writes `tend-*.yaml` workflow
files into `.github/workflows/`. Each workflow handles triggers, skip
conditions, concurrency, and permissions — then calls the composite action
(`max-sixty/tend@v1`).

The action runs security and rate-limit preflight checks, resolves bot
identity, and invokes
[claude-code-action](https://github.com/anthropics/claude-code-action) with
the tend plugin. Each workflow's prompt invokes a skill that defines what
Claude does.

Edit the config or the generator — not the workflow files. They're regenerated
on every `tend@latest init`.

## Security

Tend gives Claude write access to a repository. The security model has four
layers:

**Merge restriction** is the primary boundary. A GitHub ruleset prevents the
bot from merging to protected branches — bot-authored PRs require human
approval. `tend check` verifies this; `tend check --fix` creates the ruleset.

**Config pinning** — `claude-code-action` restores `.claude/`, `.mcp.json`,
`.claude.json`, `.gitmodules`, and `.ripgreprc` from the base branch on all
PRs (preventing startup-time code execution). Tend additionally pins
`CLAUDE.md` on fork PRs to block prompt injection from untrusted sources.

**Rate limiting** — Burst detection (10 PRs and 10 issues per 20 minutes,
checked independently) and daily spike detection halt the bot before runaway
loops cause damage.

**Fixed prompts** — Workflow prompts come from the action, not from
attacker-controlled input like PR descriptions or comments.

Full threat model: [docs/security-model.md](docs/security-model.md).

## Configuration

`.config/tend.toml` — only `bot_name` is required:

```toml
bot_name = "my-project-bot"
```

### Secrets

Two repo secrets:

| Secret                    | Value                                                                                                                       |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `BOT_TOKEN`               | Bot account PAT — classic with `repo` scope, or fine-grained with `contents:write`, `pull-requests:write`, `issues:write`   |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token (via PKCE flow, not an API key)                                                                     |

Override names if needed:

```toml
[secrets]
bot_token = "MY_BOT_PAT"
claude_token = "MY_CLAUDE_TOKEN"
```

`tend check` flags repo-level secrets not in an explicit allowlist. Repos with
additional legitimate secrets must list them:

```toml
[secrets]
allowed = ["CODECOV_TOKEN"]
```

Release secrets (registry tokens, signing keys) belong in a protected GitHub
Environment — see [security model](docs/security-model.md).

### Protected branches

The default branch is always protected. Additional branches:

```toml
protected_branches = ["v1", "v2"]
```

### Setup steps

Build tools, caches, and environment variables that run before Claude:

```toml
setup = [
  {uses = "./.github/actions/my-setup"},
  {run = "echo CARGO_TERM_COLOR=always >> $GITHUB_ENV"},
]
```

For actions needing `with:` parameters, `raw` injects verbatim YAML:

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

For complex setups, a local composite action
(`.github/actions/tend-setup/action.yaml`) referenced via `uses` is cleaner.

### Workflow overrides

```toml
[workflows.ci-fix]
watched_workflows = ["ci", "build"]   # required for ci-fix

[workflows.nightly]
cron = "0 8 * * *"                    # override default schedule
prompt = "/my-custom-nightly"         # override the default prompt
```

## Project context

Tend reads `CLAUDE.md` like any Claude Code session — build commands, test
commands, project conventions all go there.

For tend-specific guidance, add a skill overlay at
`.claude/skills/running-tend/SKILL.md`. Common uses: recording which CI
workflow names `tend-ci-fix` watches, PR title conventions, label policies.

## License

MIT
