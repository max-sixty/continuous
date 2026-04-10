# Tend — Design

## What this is

Two Claude Code plugins, a GitHub composite action, and a generator that add
Claude-powered CI to any repo. Handles PR review, issue triage, @bot mentions,
CI fixes, nightly sweeps, and weekly maintenance.

## Architecture

Four pieces:

1. **Plugins** — two Claude Code plugins from the same marketplace.
   `install-tend` is user-facing (sets up tend on a new repo).
   `tend-ci-runner` provides CI skills, installed by the composite action
   from the marketplace.

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
     system_prompt_append: { default: "...Use /tend-ci-runner:running-in-ci..." }
     allowed_bots: { default: "*" }
     allowed_non_write_users: { default: "*" }
     show_full_output: { default: "true" }
     use_sticky_comment: { default: "false" }
     additional_permissions: { default: "actions: read" }
   ```

   `bot_id` and `trigger_phrase` are derived automatically — `bot_id` via
   `gh api users/{bot_name}`, `trigger_phrase` as `@{bot_name}`.

   The action doesn't know or care about triggers, checkout, or project setup.

3. **Generator** (`uvx tend@latest init`) — stamps out workflow files into the
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

jobs:
  review:
    if: >-
      github.event.pull_request.draft == false
    runs-on: ubuntu-24.04
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
            ${{ format('/tend-ci-runner:review {0}', github.event.pull_request.number) }}
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

## Extending generated workflows

The generator owns the workflow file, but adopters can override individual
keys at the workflow level or job level via `.config/tend.toml`:

```toml
# Add top-level env vars to the review workflow
[workflows.review.workflow_extra.env]
MY_VAR = "hello"

# Override job-level keys (addressed by job name)
[workflows.review.jobs.review]
timeout-minutes = 240
runs-on = "ubuntu-22.04-large"

# Multi-job workflows: target a specific job
[workflows.mention.jobs.handle]
timeout-minutes = 180
```

Overrides use RFC 7396 (JSON Merge Patch) semantics: mappings deep-merge,
scalars and lists replace. This means an adopter can add a permission without
losing existing ones:

```toml
[workflows.review.jobs.review.permissions]
packages = "read"
# Result: packages added, contents/pull-requests/id-token/actions/issues preserved
```

When overrides are present, the generator renders the base template, parses it,
merges the overrides, and re-serializes. The output YAML formatting differs
slightly from the base template (block-style lists, quoted `'on':` key) but is
functionally identical.

**Scope:** Workflow-level (`workflow_extra`) and job-level (`jobs.<name>`)
overrides are supported. Step-level overrides are not — the existing `[[setup]]`
mechanism handles step injection, and step modification is deferred until there's
concrete demand.

**No validation of override keys.** The adopter can set anything; the generator
doesn't maintain an allowlist. Unknown job names produce a warning.

## Auth

Each adopter creates a GitHub bot account and a classic PAT (`public_repo`
for public repos, `repo` for private) plus `workflow`, `notifications`, and
`write:discussion` scopes. The PAT and a Claude OAuth token are stored as repo
secrets.

Classic PATs are all-or-nothing — `public_repo` grants full write to every
public repo the user can access. Fine-grained PATs allow per-category
scoping (`contents: read` + `pull_requests: write`) but don't support
outside collaborators (planned on [GitHub's roadmap][gh-601] but not
shipped). GitHub Apps provide real per-category permissions but require
either per-adopter App registration or tend-hosted infrastructure (see
Alternative models below).

With a classic PAT, the only way to restrict what the token can do on a
specific repo is the **collaborator level**. A `public_repo` PAT for a user
with triage access can comment and review but cannot push code — GitHub
enforces this server-side regardless of the PAT's scope.

[gh-601]: https://github.com/github/roadmap/issues/601

### Privilege models

The `mode` field in `.config/tend.toml` selects between two privilege
models (not yet implemented — currently only write + branch protection
exists):

| | **Triage + fork** | **Write + branch protection** |
|---|---|---|
| | *recommended default* | *upgrade for approvals / direct push* |
| Bot collaborator level | Triage | Write |
| Bot pushes code to | Own fork | Target repo branches |
| Creates PRs | From fork | Same-repo |
| Posts reviews / comments | Yes | Yes |
| Approvals count for required reviews | No | Yes |
| Can merge | No | No (ruleset must block) |
| Can modify target workflows | No | Yes |
| Branch protection required | **No** | **Yes** — primary security boundary |
| Leaked PAT blast radius | Comments/reviews on target; write to fork only | Full write to target repo |
| Setup complexity | Low | Medium (must configure branch protection correctly) |

Both models store two secrets: `BOT_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`.

### Triage + fork (default)

The bot account has **triage** access on the target repo and owns a fork.
The collaborator level is the security boundary — triage cannot push, merge,
or modify workflows, regardless of the PAT's scope. No branch protection or
rulesets are required.

When the bot needs to propose code changes (ci-fix, triage fix, review
response on its own PRs), it pushes to its fork and creates a cross-fork PR.
The workflow configures the fork as a separate git remote:

```bash
git remote add fork https://x-access-token:${BOT_TOKEN}@github.com/${BOT_NAME}/${REPO}.git
git push fork fix/ci-123
gh pr create --repo ${TARGET_REPO} --head ${BOT_NAME}:fix/ci-123
```

Limitations: the bot's reviews are informational (triage-level approvals
don't satisfy required review policies). Triage access prevents pushing to
human PR branches — the bot posts review suggestions instead.

### Write + branch protection

The bot account has **write** access. A merge restriction (ruleset or branch
protection) is the primary security boundary — without it the bot can merge
its own PRs. `tend check` verifies this is configured correctly.

This model adds approvals that count for required reviews, direct push to
target repo branches, and push access to human PR branches (requires the
PR author to enable "allow edits from maintainers").

*Token leak risk:* The PAT is long-lived and available to every workflow
run. A prompt injection that exfiltrates it gets permanent write access to
everything the bot account can reach. Mitigations: merge restriction caps
what the token can do, environment protection keeps release secrets safe,
periodic rotation. On the GitHub Team plan, push rulesets can block
modifications to `.github/workflows/`, preventing the
most dangerous escalation (pushing a workflow that exfiltrates repo-level
secrets).

### Anthropic token

The adopter stores their own `CLAUDE_CODE_OAUTH_TOKEN` as a repo secret.
It's passed directly from the workflow to `claude-code-action`. Each adopter
uses their own Anthropic billing. If leaked, the attacker can run Claude
sessions on the adopter's account but can't access GitHub.

### Alternative models

Both privilege models above use a classic PAT — the adopter manages the bot
account, and we never touch their repo. Below are two progressively more
managed alternatives that replace the PAT with a GitHub App.

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

## Concurrency and filtering

Events pass through up to three filtering layers before the bot does work.
Each layer can drop an event; this section documents where and why.

### Layer 1: GHA job `if:` conditions

GitHub Actions evaluates these before the job starts. A false condition means
the job is **skipped** — it never runs, never enters a concurrency group, and
consumes no resources.

| Workflow | Event | Runs when | Skips |
|----------|-------|-----------|-------|
| **review** | `pull_request_target` | PR is not a draft | Draft PRs |
| **mention** (verify) | `issues` (edited) | Issue body contains `@$bot_name` and editor is not bot | Bot's own edits; edits that don't mention bot |
| **mention** (verify) | `issue_comment` | Comment author is not bot | Bot's own comments (prevents loops) |
| **mention** (verify) | `pull_request_review_comment` | Comment author is not bot | Bot's own inline comments |
| **mention** (verify) | `pull_request_review` (submitted) | Reviewer is not bot | Bot's own reviews |
| **mention** (handle) | — | Verify job output `should_run == true` | Events where verify determined no engagement (Layer 2) |
| **triage** | `issues` (opened) | Issue author is not bot | Bot-opened issues (prevents self-triage loop) |
| **ci-fix** | `workflow_run` | Triggering workflow concluded with failure | Successful CI runs |

### Layer 2: Custom `should_run` logic (mention only)

The mention workflow has a lightweight **verify** job that checks whether the
bot should engage, before the expensive **handle** job runs. The verify job
outputs `should_run=true` or `should_run=false` based on these checks (in
order):

1. **Issue edits** (`issues` event): always `true` (the GHA condition already
   confirmed `@$bot_name` is in the body).
2. **Direct mention**: comment or review body contains `@$bot_name` → `true`.
3. **Non-mention on an issue** (not a PR): bot authored the issue, or `@$bot_name`
   appears in the issue body, or bot has previously commented → `true`.
   Otherwise → `false`.
4. **Non-mention on a PR**: bot authored the PR, or bot left reviews, or bot
   commented → `true`. Otherwise → `false`.

The handle job's `if: needs.verify.outputs.should_run == 'true'` is a GHA
condition (Layer 1), but the _decision_ is made by our custom script. A
skipped handle job never enters the concurrency group — this is critical for
the rapid-comment scenario (see below).

### Layer 3: Concurrency groups

| Workflow | Job | Group key | Cancel | Behavior |
|----------|-----|-----------|--------|----------|
| **review** | review | `workflow-PR#` | yes | New push cancels in-flight review (stale context) |
| **mention** | verify | none | — | Stateless, fast; parallel runs are harmless |
| **mention** | handle | `workflow-handle-issue#\|PR#` | **no** | Queues — each mention runs to completion (#93) |
| **triage** | triage | `workflow-issue#` | yes | Re-opened/rapid edits: latest wins |
| **ci-fix** | — | none | — | Rare overlap (failure-triggered) |
| **nightly** | — | none | — | Scheduled; GitHub serializes cron |
| **weekly** | — | none | — | Scheduled; GitHub serializes cron |

### Design rationale

**Cancel-in-progress: true** (review, triage): the cancelled run was
processing stale context. A new push invalidates a review, a new comment
supersedes triage — no work is lost.

**Cancel-in-progress: false** (mention handle): each `@$bot_name` mention is an
independent request. Cancelling a 20-minute handle run because a second mention
arrived loses work. Queuing ensures every mention is processed. The key insight
is that a `should_run=false` handle is **skipped entirely** (Layer 1) and never
enters the queue, so non-mention comments can't displace real mentions. Only
genuine `should_run=true` handles queue against each other — the correct
behavior (#93).

**No concurrency group** (ci-fix, nightly, weekly): ci-fix triggers on
workflow failure, which is rare enough that overlapping runs are unlikely.
Nightly and weekly use `schedule` + `workflow_dispatch` — GitHub serializes
cron-triggered runs, and manual dispatches are infrequent.

### Known limitation: GHA queue depth of 1

GitHub Actions concurrency groups have a pending-queue depth of exactly **one**.
With `cancel-in-progress: false`, when a third job arrives while one is running
and one is pending, the **pending job is replaced** (cancelled) by the new
arrival. The running job is unaffected.

**Mitigation — conversation-aware loading:**

The prompt preamble instructs the skill to check recent conversation before
acting:

1. **Dedup**: If the bot already responded to the triggering comment, exit
   silently.
2. **Self-heal**: If other comments warrant a response but have no bot reply,
   handle those too — oldest first.

The workflow injects the **queue-to-run time delta** (seconds between event
timestamp and job start) into the prompt. Over ~40s indicates the job was
queued behind another run, making conversation drift more likely.

## What lives in the tend repo

```
tend/
├── .claude-plugin/
│   └── marketplace.json        # Lists both plugins
├── plugins/
│   ├── install-tend/           # User-facing plugin (setup + debug skills)
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   └── skills/
│   │       ├── debug-ci-session/
│   │       └── install-tend/
│   └── tend-ci-runner/         # CI plugin (all CI skills)
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── scripts/            # Helper scripts (survey, run listing)
│       └── skills/
│           ├── ci-fix/
│           ├── nightly/
│           ├── notifications/
│           ├── review/
│           ├── review-reviewers/
│           ├── running-in-ci/
│           ├── triage/
│           └── weekly/
├── action.yaml                 # Composite action (the interface)
├── generator/                  # Python package (uvx tend@latest init)
│   ├── pyproject.toml
│   └── src/tend/
├── docs/
│   └── security-model.md
└── README.md
```

The repo hosts two Claude Code plugins and a GitHub composite action. The
`install-tend` plugin is for users setting up tend on a new repo. The
`tend-ci-runner` plugin provides CI skills installed by the composite action
from the marketplace. Users should only install `install-tend` manually.

## Security

See [`docs/security-model.md`](docs/security-model.md) for the full threat
model, current mitigations, remaining risks, and deferred hardening options.
