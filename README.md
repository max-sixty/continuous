<h1><img src="assets/logo-512.png" alt="tend logo" width="50" align="absmiddle">Tend</h1>

[![PyPI](https://img.shields.io/pypi/v/tend?style=for-the-badge&logo=pypi&logoColor=white)](https://pypi.org/project/tend/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=for-the-badge)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/github/actions/workflow/status/max-sixty/tend/ci.yaml?event=push&branch=main&style=for-the-badge&logo=github)](https://github.com/max-sixty/tend/actions?query=branch%3Amain+workflow%3Aci)
[![maintained with tend](https://img.shields.io/badge/maintained_with-tend-bba580?style=for-the-badge&logo=data:image/svg%2bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxNiAxNiI+PGcgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMCwxNikgc2NhbGUoMC4wMTI1LC0wLjAxMjUpIiBmaWxsPSIjZmZmIiBzdHJva2U9Im5vbmUiPjxwYXRoIGQ9Ik02ODAgMTEyOCBjNjIgLTk2IDY5IC0xNzggMjAgLTI0MSAtMTcgLTIyIC0yMCAtNDAgLTIwIC0xMzQgbDEgLTEwOCAyMSAyOCBjMTEgMTYgMzAgNDcgNDIgNzAgMTIgMjIgMzIgNDkgNDYgNTkgMzcgMjcgMTE0IDM4IDE4NCAyNyA5MyAtMTUgOTQgLTE4IDQ0IC03OSAtNzIgLTg4IC0xMDkgLTExMyAtMTc2IC0xMTcgLTMxIC0yIC02NCAxIC03MiA2IC0yMyAxNSAyMSA1NiAxMDcgOTggNDAgMjAgNzEgMzggNjkgNDAgLTYgNyAtODggLTE3IC0xMjYgLTM3IC00OSAtMjUgLTEwMCAtNzggLTEyMSAtMTI1IC0xNSAtMzMgLTE5IC02NiAtMTkgLTE4OCAwIC0xNTcgOCAtMTk1IDUwIC0yMzIgMTcgLTE2IDM2IC0yMCA4NSAtMTkgNjIgMSA2MyAxIDczIC0zMiA5IC0zMiA5IC0zMyAtMjIgLTQwIC01MCAtMTIgLTEzMiAtNyAtMTY0IDEwIC00MCAyMSAtNzkgNjkgLTkyIDExNCAtNSAyMCAtMTAgMTAyIC0xMCAxODIgMCA4MCAtNSAxNjIgLTExIDE4NCAtMjIgNzkgLTEzNSAxNjYgLTIzNCAxODEgLTM3IDYgLTM1IDMgMzAgLTI4IDc4IC0zOSAxNDQgLTkxIDEzMiAtMTA0IC01IC00IC0zNyAtOCAtNzEgLTggLTc3IDAgLTExNyAyNCAtMTgyIDEwOSAtNTIgNjggLTUxIDcwIDQyIDg1IDcxIDExIDE0MyAwIDE4MyAtMjkgMTYgLTExIDQwIC00MyA1NCAtNzMgMTMgLTI5IDMyIC01OSA0MSAtNjYgMTQgLTEyIDE2IC03IDE2IDU4IDAgNTkgNCA3NyAyMyAxMDIgMTkgMjYgMjMgNDYgMjUgMTMwIDMgNjcgMCA5OSAtNyA5OSAtNyAwIC0xMSAtMjMgLTEyIC01NyAwIC0zMiAtNiAtNzYgLTEyIC05NyBsLTEyIC00MCAtMjcgMzIgYy0zNCA0MSAtNDMgOTYgLTI0IDE1MSAxNCA0MSA3NSAxNDEgODYgMTQxIDMgMCAyMSAtMjQgNDAgLTUyeiIvPjwvZz48L3N2Zz4K)](https://github.com/max-sixty/tend)

<!-- [![Stars](https://img.shields.io/github/stars/max-sixty/tend?style=for-the-badge&logo=github)](https://github.com/max-sixty/tend/stargazers) -->

Tend allows open-source projects to have an agent as a dutiful junior
maintainer. The agent can review PRs, triage issues, fix CI, help out with
research, maintain a changelog, sweep the repo for improvements, refine
documentation, etc.

> Current status: Tend is in its early days. It has been working _extremely_ well in
> [Worktrunk](https://www.github.com/max-sixty/worktrunk) for the past couple of
> months, such that folks suggested I generalize it into its own project.

## Structure

To use Tend, a project needs:

- A GitHub account for the agent (for example this project's is **[@tend-agent](https://www.github.com/tend-agent))**
- One of:
  - A Claude Max subscription (harness = "claude")
  - An OpenAI API key (harness = "codex"). A ChatGPT subscription via
    a Codex `auth.json` is **not** compatible with tend's concurrent
    workflows — see [Codex (experimental alternative)](#codex-experimental-alternative).

Tend offers the default code & guidance for the agent. Specifically that means:

- A set of workflow templates
- A very particular set of Skills
  - ...skills it has acquired over a very long career (two months)

Each project's agent remains completely under its control, and runs only in the
project's Github Actions environment. The Tend project never sees any tokens /
keys / etc.

<!-- TODO: add screenshot of a review comment, triage response, or CI fix PR -->

## Quick start

The easiest way to get started is to install the Tend plugin into a local Claude
Code session, and run the [`/install-tend` skill](plugins/install-tend/skills/install-tend/SKILL.md):

```sh
claude plugin marketplace add max-sixty/tend
claude plugin install install-tend@tend
claude /install-tend
```

It'll take 5-15 minutes to set up the config, workflow generation, bot account,
secrets, and branch protection. Tend is configured through a [config
file](docs/tend.example.yaml) and a repo-local `/running-tend` skill.

## Reasons _not_ to use Tend

- Tend uses lots of tokens. A Claude subscription, an Anthropic API key,
  an OpenAI API key, or a ChatGPT plan is needed to fund the runs.
  - Maintainers of sizeable OSS projects [get a 20x Claude Max subscription
    for free from
    Anthropic](https://claude.com/contact-sales/claude-for-oss).
- A compromise of the runner or credential proxy could expose the bot PAT or
  model credential. The agent cannot read them during normal operation, and
  the merge restriction, environment gate, and immutable releases limit what a
  stolen bot credential can do.

## Workflows

| Workflow          | Trigger                    | What happens                                                                                                                                                |
| ----------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **review**        | PR opened/updated          | Reviews for correctness and duplication. Traces error paths. Monitors CI. Pushes fixes to bot-authored PRs.                                                 |
| **mention**       | @bot mention, review       | Responds to requests in PR and issue conversations.                                                                                                         |
| **triage**        | Issue opened               | Classifies the issue, checks for duplicates, reproduces bugs, attempts conservative fixes.                                                                  |
| **ci-fix**        | CI fails or is cancelled   | Diagnoses the unsuccessful default-branch run, searches for the same pattern elsewhere, and opens a fix PR when needed.                                  |
| **nightly**       | Daily                      | Resolves conflicts on open PRs, reviews recent commits, surveys ~10 files for bugs and stale docs, closes resolved issues, regenerates tend workflow files. |
| **weekly**        | Weekly                     | Reviews dependency PRs, approves safe patch and minor updates (the bot never merges — a merge restriction is the security boundary).                        |
| **notifications** | Every 15 minutes           | Drains unread notifications as a recovery queue and repairs conflicts on bot-authored PRs.                                                                  |
| **review-runs**   | Daily                      | Reviews recent CI runs for behavioral problems and proposes skill/config improvements.                                                                      |

The bot reacts 👀 while a session is working: on an issue when it opens, on a
PR whenever a review starts, and on a comment that mentions the bot. The
reaction comes off when the session ends.

Scheduled workflows also support manual dispatch for testing. GitHub runs
`schedule` triggers on a best-effort basis and drops ticks under load, so
the intervals above are the requested cadence rather than a guarantee —
observed gaps between runs are routinely longer. All are generated by
default except **ci-fix**, which requires `watched_workflows` to be
configured. Any can be omitted on the next regeneration:

```yaml
workflows:
  weekly:
    enabled: false
```

## How it works

`uvx tend@latest init` reads `.config/tend.yaml` and writes `tend-*.yaml` workflow
files into `.github/workflows/`. Each workflow handles triggers, skip
conditions, concurrency, and permissions — then calls the composite action
for the configured harness, pinned to the released generator version
(`max-sixty/tend/claude@X.Y.Z` for Claude, `max-sixty/tend/codex@X.Y.Z` for Codex).
The nightly regen restamps a newer tag when a new tend version ships.

When the review workflow is generated, `init` also merges one ignore into
`.github/actionlint.yaml`: the workflow's `concurrency.queue` is valid GitHub
syntax that actionlint's schema rejects. The ignore applies only to generated
workflows and preserves the rest of the adopter-owned config.

Both actions run the same security and rate-limit preflight checks and
resolve bot identity. They differ in how the agent runs:

- **Claude harness** — runs the official `claude` binary headless
  (`claude -p`) as a non-sudo sandbox user behind a local
  credential-injecting proxy, so the bot token and Anthropic credential
  never enter the agent's environment. Each workflow's prompt is a slash
  command (`/tend-ci-runner:review`) that loads the matching skill.
- **Codex harness** — installs the `@openai/codex` CLI, then runs
  `codex exec` as the same non-sudo sandbox user. GitHub calls use Tend's
  exact-host proxy; model calls use OpenAI's narrow Responses API proxy.
  An AGENTS.md staged into `$CODEX_HOME` teaches Codex to resolve
  `/tend-ci-runner:NAME` references to the bundled skill markdown.

Edit the config or the generator — not the workflow files. They're regenerated
on every `tend@latest init`.

## Security

Tend gives an agent write access to a repository. The security model has seven
layers:

**Merge restriction** is the primary boundary. A GitHub ruleset prevents the
bot from merging to protected branches — bot-authored PRs require human
approval. The bot proves this against itself on every run: preflight asks
GitHub whether the bot's own credentials can bypass any ruleset covering the
default branch (`current_user_can_bypass` — GitHub's evaluation, so teams,
custom roles, and org-level rulesets are all accounted for) and refuses to
start unless the answer is no. `tend check` verifies the setup;
`tend check --fix` creates the ruleset.

**Immutable releases** lock each release published after the setting is
enabled, including its assets and tag. `tend check` requires the setting and
`--fix` enables it before the next release.

**Environment-gated credentials** — a workflow the bot can cause to run
reaches no credential: not the bot token, not the model auth, not a release
token or a trusted-publishing identity. The bot token and model auth live in
the repo's `tend` GitHub Environment, whose deployment policy admits only
the refs `tend check` confirmed the merge restriction covers, so a workflow
pushed to any other branch is refused them before its first step. The same
check sweeps every other credential-holding environment — one that stores a
secret, or that a job requesting `id-token: write` deploys to — for a gate
the bot cannot pass: a non-bot required reviewer, or a policy naming only
verified refs that no workflow reaches on a trigger the bot both fires and
steers. It flags any repo-level secret not explicitly listed in
`secrets.allowed`, where the operational names are refused outright.
`tend check --fix` creates the environment and sets its policy; moving the
secrets into it stays manual — their values can't be read back.

**Credential isolation** — both harnesses run the agent as a separate non-sudo
user. Tend's exact-host proxy holds the bot token; Claude model auth uses the
same mechanism, while Codex model auth uses OpenAI's proxy that accepts only
Responses API calls. The proxies authenticate the agent without placing the
real credentials in its environment. GitHub authentication applies to any
repository the bot account can access, including repositories other than the
one that started the run.

**Config pinning** — the action restores `.mcp.json`, `.claude.json`,
`.gitmodules`, `.ripgreprc`, `.husky`, and every `CLAUDE.md`, `CLAUDE.local.md`,
`AGENTS.md`, and `.claude/` in the tree, at any depth, from the base branch
before the agent starts, blocking both startup-time code execution and prompt
injection from a PR's own copy of those files.

**Rate limiting** — Burst detection (10 PRs and 10 issues per 20 minutes,
checked independently) and daily spike detection halt the bot before runaway
loops cause damage.

**Fixed prompts** — Workflow prompts come from the action, not from
attacker-controlled input like PR descriptions or comments.

Full threat model: [docs/security-model.md](docs/security-model.md).

## Configuration

`.config/tend.yaml` — only `bot_name` is required. The default harness runs
Claude; `harness: codex` selects OpenAI Codex (see
[Harnesses](#harnesses) below).

```yaml
bot_name: my-project-bot

# Optional runtime switch — every new job skips before checkout or setup
# enabled: false

# Optional — defaults to "claude"
# harness: codex
# effort: medium   # low | medium | high | xhigh; Claude Opus/Sonnet also support max
# args: [--max-turns, "40"]   # exact additional CLI arguments
```

Top-level `enabled: false` pauses tend from the default branch without removing
its workflows. Setting it back to `true` (or removing it) resumes new jobs
without regeneration.

The secrets, stored in the repo's `tend` environment (install-tend creates
it; `tend check` verifies it), depend on the harness:

| Harness    | Required secrets                                                                                                         |
| ---------- | ----------------------------------------------------------------------------------------------------------------------- |
| `claude`   | `TEND_BOT_TOKEN` + one of `CLAUDE_CODE_OAUTH_TOKEN` (subscription) or `ANTHROPIC_API_KEY` (API-billed)                   |
| `codex`    | `TEND_BOT_TOKEN` + `OPENAI_API_KEY` (pay-per-token).                                                                    |

`TEND_BOT_TOKEN` is the bot account's PAT — see
[example config](docs/tend.example.yaml) for scopes.
`CLAUDE_CODE_OAUTH_TOKEN` is from `claude setup-token`. The other two
are standard API keys from console.anthropic.com and
platform.openai.com. See [Codex (experimental alternative)](#codex-experimental-alternative) for
why the Codex subscription `auth.json` path isn't supported;
[docs/security-model.md](docs/security-model.md) has the full leak
breakdown.

All other options — setup steps, protected branches, workflow overrides,
schedules — are documented in
[`docs/tend.example.yaml`](docs/tend.example.yaml).

## Project context

Tend reads `CLAUDE.md` like any Claude Code session — build commands, test
commands, project conventions all go there.

For tend-specific guidance, add a skill overlay at
`.claude/skills/running-tend/SKILL.md`. Common uses: recording which CI
workflow names `tend-ci-fix` watches, PR title conventions, label policies.

## Harnesses

Tend supports Claude and Codex. Pick whichever fits the credentials and
billing path that already work for you; both run the same workflows and
skills.

### Claude (default)

Runs the official `claude` binary headless (`claude -p`) as a non-sudo
sandbox user behind a local credential-injecting proxy: the bot token and
the Anthropic credential live only in the proxy, never in the agent's
environment. Two auth modes:

- **`CLAUDE_CODE_OAUTH_TOKEN`** (recommended with a Claude
  subscription) — Claude Code OAuth token from `claude setup-token`,
  funded by the subscription's usage limits.
- **`ANTHROPIC_API_KEY`** — standard API key from console.anthropic.com,
  billed per token against the Console org. Pick this when there's no
  Claude subscription, when the bot should bill against a dedicated
  Console org, or when per-key revocation matters.

The proxy injects whichever you set into requests to api.anthropic.com; the
agent itself only ever holds a dummy.

### Codex (experimental alternative)

Installs `@openai/codex` and invokes `codex exec` as the non-sudo sandbox user.
GitHub access goes through Tend's exact-host proxy; the OpenAI key is read from
stdin by OpenAI's narrow Responses API proxy and is never placed in the agent's
environment. A bundled `AGENTS.md` teaches Codex to resolve tend's slash
commands to skill markdown.

Codex currently runs with `danger-full-access` inside the ephemeral runner.
Its restricted Linux modes cannot initialize bubblewrap's loopback network on
the standard GitHub-hosted Ubuntu 24.04 runner; the separate UID and credential
proxies remain the credential boundary.

Use `OPENAI_API_KEY` (a standard OpenAI API key, pay-per-token, from
platform.openai.com). Works for any repo, public or private.

> **Subscription `auth.json` is not supported.** Codex rotates that
> refresh token on every API call and invalidates the prior token; tend
> runs multiple workflows concurrently (review, mention, triage,
> nightly, …), so each call would invalidate the credential the other
> in-flight jobs are using. A scheduled refresher works around the
> ~8-day rotation but not the per-call invalidation between concurrent
> jobs. Use `OPENAI_API_KEY` instead.

## Badge

A badge signals the repo is maintained with tend:

```markdown
[![maintained with tend](https://img.shields.io/badge/maintained_with-tend-bba580?logo=data:image/svg%2bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxNiAxNiI+PGcgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMCwxNikgc2NhbGUoMC4wMTI1LC0wLjAxMjUpIiBmaWxsPSIjZmZmIiBzdHJva2U9Im5vbmUiPjxwYXRoIGQ9Ik02ODAgMTEyOCBjNjIgLTk2IDY5IC0xNzggMjAgLTI0MSAtMTcgLTIyIC0yMCAtNDAgLTIwIC0xMzQgbDEgLTEwOCAyMSAyOCBjMTEgMTYgMzAgNDcgNDIgNzAgMTIgMjIgMzIgNDkgNDYgNTkgMzcgMjcgMTE0IDM4IDE4NCAyNyA5MyAtMTUgOTQgLTE4IDQ0IC03OSAtNzIgLTg4IC0xMDkgLTExMyAtMTc2IC0xMTcgLTMxIC0yIC02NCAxIC03MiA2IC0yMyAxNSAyMSA1NiAxMDcgOTggNDAgMjAgNzEgMzggNjkgNDAgLTYgNyAtODggLTE3IC0xMjYgLTM3IC00OSAtMjUgLTEwMCAtNzggLTEyMSAtMTI1IC0xNSAtMzMgLTE5IC02NiAtMTkgLTE4OCAwIC0xNTcgOCAtMTk1IDUwIC0yMzIgMTcgLTE2IDM2IC0yMCA4NSAtMTkgNjIgMSA2MyAxIDczIC0zMiA5IC0zMiA5IC0zMyAtMjIgLTQwIC01MCAtMTIgLTEzMiAtNyAtMTY0IDEwIC00MCAyMSAtNzkgNjkgLTkyIDExNCAtNSAyMCAtMTAgMTAyIC0xMCAxODIgMCA4MCAtNSAxNjIgLTExIDE4NCAtMjIgNzkgLTEzNSAxNjYgLTIzNCAxODEgLTM3IDYgLTM1IDMgMzAgLTI4IDc4IC0zOSAxNDQgLTkxIDEzMiAtMTA0IC01IC00IC0zNyAtOCAtNzEgLTggLTc3IDAgLTExNyAyNCAtMTgyIDEwOSAtNTIgNjggLTUxIDcwIDQyIDg1IDcxIDExIDE0MyAwIDE4MyAtMjkgMTYgLTExIDQwIC00MyA1NCAtNzMgMTMgLTI5IDMyIC01OSA0MSAtNjYgMTQgLTEyIDE2IC03IDE2IDU4IDAgNTkgNCA3NyAyMyAxMDIgMTkgMjYgMjMgNDYgMjUgMTMwIDMgNjcgMCA5OSAtNyA5OSAtNyAwIC0xMSAtMjMgLTEyIC01NyAwIC0zMiAtNiAtNzYgLTEyIC05NyBsLTEyIC00MCAtMjcgMzIgYy0zNCA0MSAtNDMgOTYgLTI0IDE1MSAxNCA0MSA3NSAxNDEgODYgMTQxIDMgMCAyMSAtMjQgNDAgLTUyeiIvPjwvZz48L3N2Zz4K)](https://github.com/max-sixty/tend)
```

The install-tend skill adds this automatically during setup; the kickoff
prompt lets you opt out.

## License

MIT
