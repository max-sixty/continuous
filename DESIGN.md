# Tend — Design

## What this is

A Claude Code plugin, GitHub composite action, and generator that adds
Claude-powered CI to any repo. Handles PR review, issue triage, @bot mentions,
CI fixes, nightly sweeps, and dependency updates.

## Architecture

Four pieces:

1. **Plugin** (`tend`) — Claude Code plugin providing CI skills. Adopters
   install the plugin (`claude plugin add max-sixty/tend`), which makes
   the skills available to Claude Code in all environments including CI.

2. **Composite action** (`max-sixty/tend@v1`) — the stable interface.
   Resolves the bot's numeric ID at runtime, invokes `claude-code-action`,
   uploads session logs. Inputs:

   ```yaml
   inputs:
     github_token: { required: true }
     claude_code_oauth_token: { required: true }
     bot_name: { required: true }
     prompt: { required: true }
     model: { default: "opus" }
     allowed_tools: { default: "Bash,Edit,Read,Write,Glob,Grep,WebSearch,WebFetch,Task,Skill" }
     system_prompt_append: { default: "...Use /tend:tend-running-in-ci..." }
     allowed_bots: { default: "*" }
     allowed_non_write_users: { default: "*" }
     show_full_output: { default: "true" }
     use_sticky_comment: { default: "false" }
     additional_permissions: { default: "actions: read" }
   ```

   `bot_id` and `trigger_phrase` are derived automatically — `bot_id` via
   `gh api users/{bot_name}`, `trigger_phrase` as `@{bot_name}`.

   The action doesn't know or care about triggers, checkout, or project setup.

3. **Generator** (`uvx tend init`) — stamps out workflow files into the
   adopter's `.github/workflows/`. These contain the trigger events, `if:`
   conditions, engagement verification, concurrency groups, checkout, project
   setup steps, and the call to the composite action. The adopter commits the
   generated files. Generation is idempotent — running `init` again overwrites
   all files from the current config.

4. **Config** (`.config/tend.toml`) — stores the inputs to the generator.
   Only overrides from defaults need to be specified. All six workflows are
   enabled by default.

   Minimal example (worktrunk):

   ```toml
   bot_name = "worktrunk-bot"

   [secrets]
   bot_token = "WORKTRUNK_BOT_TOKEN"
   claude_token = "CLAUDE_CODE_OAUTH_TOKEN"

   [setup]
   uses = ["./.github/actions/claude-setup"]

   [workflows.ci-fix]
   watched_workflows = ["ci", "publish-docs"]
   ```

## What the adopter's workflow looks like

Generated workflows are standalone — full `steps:` jobs, not `workflow_call`.
The generator owns the entire file. Project setup (build tools, caches, env
vars) is defined in the `[setup]` section of the config and rendered into each
workflow.

```yaml
# .github/workflows/tend-review.yaml (generated)
name: tend-review
on:
  pull_request_target:
    types: [opened, synchronize, ready_for_review, reopened]
  pull_request_review:
    types: [submitted]

jobs:
  review:
    if: |
      (github.event_name == 'pull_request_target' &&
        github.event.pull_request.draft == false) ||
      (github.event_name == 'pull_request_review' && ...)
    runs-on: ubuntu-24.04
    timeout-minutes: 60
    permissions:
      contents: write
      pull-requests: write
      id-token: write
      actions: read
      issues: write
    steps:
      - uses: actions/checkout@v6
        with:
          ref: refs/pull/${{ github.event.pull_request.number }}/merge
          fetch-depth: 0
          token: ${{ secrets.WORKTRUNK_BOT_TOKEN }}

      - uses: ./.github/actions/claude-setup

      - uses: max-sixty/tend@v1
        with:
          github_token: ${{ secrets.WORKTRUNK_BOT_TOKEN }}
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          bot_name: worktrunk-bot
          prompt: >-
            ${{ format('/tend:tend-review {0}', github.event.pull_request.number) }}
```

## What the generator owns vs what the adopter owns

| Aspect | Owner | Lives in |
|--------|-------|----------|
| Trigger events (`on:`) | Generator | generated workflow |
| Filter conditions (`if:`) | Generator | generated workflow |
| Engagement verification (mention) | Generator | generated workflow |
| Concurrency groups | Generator | generated workflow |
| Permissions | Generator | generated workflow |
| Checkout | Generator | generated workflow |
| Project setup (build tools, cache) | Adopter | `[setup]` in `.config/tend.toml` |
| Composite action call | Generator | generated workflow |
| Bot identity, auth config | Adopter | `.config/tend.toml` |
| Skills (generic) | Tend | `tend` plugin (marketplace) |
| Skills (project-specific) | Adopter | `.claude/skills/` in their repo |

## Auth

Each adopter creates a GitHub bot account and generates a PAT with
`contents:write`, `pull-requests:write`, `issues:write`. The PAT and a Claude
OAuth token are stored as repo secrets. We never see either token.

*Token leak risk:* The PAT is long-lived and available to every workflow run.
A prompt injection that exfiltrates it gets permanent write access to
everything the bot account can reach (not just the current repo, unless the
bot account is scoped to one repo). Mitigations: merge restriction (ruleset)
caps what the token can do, environment protection keeps release secrets
safe, periodic rotation limits exposure window.

*Anthropic token:* The adopter stores their own `CLAUDE_CODE_OAUTH_TOKEN`
as a repo secret. It's passed directly from the workflow to
`claude-code-action`. Each adopter uses their own Anthropic billing. If
leaked, the attacker can run Claude sessions on the adopter's account but
can't access GitHub.

### Alternative models

The design above (composite action + generator + PAT) optimizes for
simplicity and zero trust — we never touch the adopter's repo. Below are
two progressively more managed alternatives.

**Workflow verbosity across models:**

| Model | Workflow files in adopter's repo | Lines per workflow (approx) |
|-------|--------------------------------|----------------------------|
| Default (PAT) | 6 generated files | 30-80 each (mention is largest due to engagement verification) |
| Model A | Same files, same size | Same — only the auth step changes (PAT secret → OIDC call) |
| Model B | None | Zero — logic lives in our service |

**Model A: Token-minting service.**

The adopter's experience:

1. Install our GitHub App on their repo
2. Run `tend init` (generates workflow files, no GitHub secrets to
   configure — only their Claude token as a repo secret)
3. Push the generated workflows. Done.

*Where workflows live:* In the adopter's repo, generated by `tend
init`. Same as the default model.

*Where triggers are defined:* In those generated workflow files — same `on:`
blocks, `if:` conditions, and engagement verification scripts as the default
model.

*Where workflows run:* On the adopter's GitHub Actions runners, same as the
default model. The only difference is how the GitHub token is obtained — an
OIDC call to our service replaces reading a PAT from repo secrets.

We register a GitHub App and hold its private key — same as any GitHub
App (Codecov, Renovate, etc.). The adopter installs the App on their repo,
granting it the permissions it requests. This is the standard GitHub App
trust model: the adopter trusts the App by installing it, like installing
any third-party App.

Each workflow run authenticates to our service via GitHub's OIDC token
(`id-token: write`), which proves the caller's repo identity. Our service
mints a scoped installation token (~1h lifetime) for that repo and
returns it.

```yaml
- uses: max-sixty/tend/auth@v1  # OIDC → our service → scoped token
  id: auth
- uses: actions/checkout@v6
  with:
    token: ${{ steps.auth.outputs.token }}
- uses: ./.github/actions/project-setup
- uses: max-sixty/tend@v1
  with:
    github_token: ${{ steps.auth.outputs.token }}
    claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

*Token leak risk:* The GitHub token minted during a workflow run is scoped
to that single repo and expires in ~1h. A prompt injection that exfiltrates
it gets temporary, single-repo write access — vs the default model's PAT
which is permanent and potentially multi-repo.

*Anthropic token:* The adopter stores their own `CLAUDE_CODE_OAUTH_TOKEN` as
a repo secret. We never see it — it's passed directly from the workflow to
`claude-code-action`. Each adopter uses their own Anthropic billing.

Model A could be extended to push workflow updates via PR, but this
requires a webhook handler to detect config changes.

**Model B: Full webhook handler.**

The adopter's experience:

1. Install our GitHub App
2. Add `.config/tend.toml` to their repo
3. Done. No workflow files.

*Where workflows live:* Nowhere in the adopter's repo. The logic lives
in our service.

*Where triggers are defined:* In our webhook handler. GitHub sends raw
events (PR opened, comment created, etc.) to our service. We decide
which events to act on, handle engagement verification, manage
concurrency — all the logic that lives in generated workflow files in
the other models lives in our code instead.

*Where workflows run:* On our infrastructure (or dispatched back to the
adopter's runners — see Anthropic token options below). We check out the
adopter's repo, install skills, and run Claude.

This is the most cohesive UX. It also partially addresses the fork PR gap:
we receive all webhooks (including inline review comments on fork PRs) and
can respond via the API regardless of fork status. Pushing code to fork
branches still requires the fork author to enable maintainer edits.

*Token leak risk:* Same App key arrangement as Model A, but here our
service also executes code in the context of the adopter's repo. A compromise of our
infrastructure exposes write access to every adopter's repo and their
code. This is a fundamentally different trust model — the adopter trusts
us with code execution, not just token minting.

*Anthropic token:* This is the hard one. Options:

- *Adopter provides token to our service* (via dashboard or encrypted
  config). We hold it and use it to run Claude sessions. The adopter
  trusts us with their Anthropic billing. If our service is compromised,
  the attacker gets every adopter's Claude token.
- *We provide Claude access* and bill the adopter. We hold a single
  Anthropic account, run all sessions, and charge adopters for usage.
  Simpler for adopters (no Anthropic account needed) but we take on
  billing and usage management.
- *Adopter runs Claude on their own runners* via `workflow_dispatch`.
  Our service receives webhooks and dispatches back to the adopter's
  repo. The Claude token stays in the adopter's repo secrets, never
  reaches our service. This hybrid keeps the Anthropic token fully
  under adopter control but adds latency (webhook → our service →
  workflow_dispatch → runner) and complexity.

## What lives in the tend repo

```
tend/
├── .claude-plugin/
│   └── plugin.json         # Plugin manifest
├── skills/                 # CI skills (distributed via plugin)
│   ├── tend-running-in-ci/
│   ├── tend-review/
│   ├── tend-triage/
│   ├── tend-ci-fix/
│   ├── tend-nightly/
│   ├── tend-renovate/
│   └── tend-review-reviewers/
├── action.yaml             # Composite action (the interface)
├── scripts/                # Helper scripts installed by the action
├── generator/              # Python package (uvx tend init)
│   ├── pyproject.toml
│   └── src/tend/
├── docs/
│   └── security-model.md
└── README.md
```

The repo serves two roles: a Claude Code plugin (skills) and a GitHub composite
action (runtime). The plugin is installed by adopters from the marketplace; the
action is referenced in generated workflows.
