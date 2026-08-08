# Development

Tend is an autonomous CI maintainer for GitHub repos: it reviews PRs, triages
issues, and fixes CI, powered by Claude or Codex. This repo ships the generator
(`uvx tend@latest`) that stamps each adopter's workflow files, plus the plugins
and composite actions those workflows run.

No backward compatibility. When a config format or API changes, cut over
completely — old formats should fail with a clear error, not silently parse.

## Commands

```bash
wt test                            # every suite (generator/, proxy/, install-tend scripts, worker/)
uvx tend@latest init               # regenerate workflows from .config/tend.yaml
uvx tend@latest init --dry-run     # preview without writing
uvx tend@latest check              # verify branch protection, secrets, bot access
pre-commit run --all-files         # lint: ruff, typos, actionlint, shellcheck, uv-lock
```

`wt test` runs [`dev/test.sh`](dev/test.sh), which mirrors the test jobs in
`ci.yaml`; arguments go to the pytest suites (`wt test -k render`).

## Architecture

Four pieces:

1. **Plugins** — `install-tend` (user-facing setup) and `tend-ci-runner` (CI
   skills). Both ship from the same marketplace.
2. **Composite action(s)** — the stable interface, pinned to an immutable
   release tag (`max-sixty/tend/<harness>@X.Y.Z`, the generator's own version
   — no floating `v1`). Every harness lives under a harness-named path; there
   is no bare-root default. One per harness:
   - `max-sixty/tend/claude@X.Y.Z` (Claude) — runs the official `claude`
     binary headless (`claude -p`) as a non-sudo sandbox user behind the
     credential-injecting proxy; completion is the process exit code.
     Inputs in `claude/action.yaml`.
   - `max-sixty/tend/codex@X.Y.Z` (Codex) — installs `@openai/codex` and
     shells out to `codex exec`. Skills are staged on disk and an
     `AGENTS.md` in `$CODEX_HOME` teaches Codex to resolve
     `/tend-ci-runner:NAME` slash commands. Inputs in `codex/action.yaml`.
     Shares the cross-harness preflight/teardown scripts under `shared/steps/`.

   All actions resolve the bot's numeric ID at runtime, run security and
   rate-limit preflight, and upload session logs. The actions don't know
   or care about triggers, checkout, or project setup.

   Removed: `claude-interactive`, a PTY-supervised variant of the same binary
   that existed only to dodge the 2026-06-15 Agent SDK metering (which covered
   `claude -p` but not interactive sessions). Anthropic paused that change and
   the default harness now runs the binary rather than the SDK, so nothing
   selected it. Restore from `036f9c4` if the metering resumes.
3. **Generator** (`uvx tend@latest init`) — stamps workflow files into
   the adopter's `.github/workflows/` from `.config/tend.yaml`. Picks the
   right action ref and secret names per `harness`. Generation is
   idempotent — running `init` again overwrites all files from the
   current config.
4. **Config** (`.config/tend.yaml`) — inputs to the generator. Overrides
   from defaults only. `harness: claude | codex` selects the harness
   (default `claude`). A per-workflow `harness:` override (and matching
   `model:`) lets an adopter trial a different harness on one workflow at a
   time. All workflows are enabled by default.

Generated workflows are standalone — full `steps:` jobs, not
`workflow_call`. The generator owns the entire file. Project setup (build
tools, caches, env vars) is defined in the `setup:` section of the config
and rendered into each workflow.

## Structure

```
tend/
├── .claude-plugin/
│   └── marketplace.json  # Claude Code marketplace — lists both plugins
├── .agents/plugins/
│   └── marketplace.json  # Codex marketplace — lists tend-ci-runner
├── plugins/
│   ├── install-tend/     # User-facing plugin (setup skill)
│   └── tend-ci-runner/   # CI plugin (review, triage, ci-fix, etc.)
│       ├── .codex-plugin/  # Codex plugin manifest
│       └── scripts/      # Helper scripts (survey, run listing)
├── claude/
│   └── action.yaml       # Claude harness composite action (default, headless)
├── codex/
│   ├── action.yaml       # Codex harness composite action
│   └── agents-tail.md    # AGENTS.md appendix for Codex
├── shared/
│   ├── steps/            # Shared composite-action step bodies (bash scripts)
│   └── system-prompt.md  # Harness-neutral system prompt base
├── proxy/                # Credential-injection proxy (setup-sandbox.sh, addon)
├── generator/            # Python package (uvx tend@latest), uv_build backend
│   ├── src/tend/
│   │   ├── config.py     # Reads .config/tend.yaml
│   │   ├── workflows.py  # Generates workflow YAML
│   │   ├── checks.py     # Security checks (branch protection, secrets)
│   │   └── cli.py        # Click CLI (init, check)
│   └── tests/
├── site/                 # Astro marketing site (tend-src.com)
├── worker/               # Cloudflare Worker — serves the 2 site data streams
├── data/                 # consumers.json — Worker's input (refreshed weekly)
└── docs/
    └── security-model.md
```

## Key details

The `tend-*.yaml` files in `.github/workflows/` are generated by `uvx tend@latest init`
from `.config/tend.yaml`. Edit the generator or config, not the workflow files
directly.

The generator is a Python package under `generator/` — uses the uv_build
backend, requires Python 3.11+. Runtime dependencies: click, jinja2,
ruamel.yaml. Dev dependencies: pytest, pytest-regtest.

Consuming repos regenerate their `tend-*.yaml` workflows nightly (tend itself
included — it dogfoods its own workflows). Changes to the generator do not
require manual regeneration in downstream repos.

Tend's own `tend-*.yaml` workflows track the latest published release. They
update each night via `uvx tend@latest init`. Updating earlier to the latest
release (e.g., during a release commit) is fine. Never regenerate them with
the in-tree generator: the action ref is pinned to the generator's own
version (`max-sixty/tend/<harness>@X.Y.Z`), so an unreleased in-tree version
stamps a tag that does not exist yet, and the workflow's `uses:` fails to resolve.
Between a generator commit and the next release the committed workflows lag
the in-tree generator; that is expected, and the gap closes at the next
release (which tags `X.Y.Z` before regenerating, so the pin always resolves).

## Generator vs adopter ownership

| Aspect | Owner | Lives in |
|---|---|---|
| Trigger events (`on:`) | Generator | generated workflow |
| Filter conditions (`if:`) | Generator | generated workflow |
| Engagement verification (mention) | Generator | generated workflow |
| Concurrency groups | Generator | generated workflow |
| Permissions | Generator | generated workflow |
| Checkout | Generator | generated workflow |
| Composite action call | Generator | generated workflow |
| Project setup (build tools, cache) | Adopter | `setup:` in `.config/tend.yaml` |
| Bot identity, auth config | Adopter | `.config/tend.yaml` |
| Skills (generic) | Tend | `tend` plugin (marketplace) |
| Skills (project-specific) | Adopter | `.claude/skills/` in their repo |

## Workflow overrides

Adopters extend generated workflows via YAML keys that the generator
merges into the rendered YAML using RFC 7396 (JSON Merge Patch — mappings
deep-merge, scalars and lists replace, `null` deletes):

```yaml
workflows:
  review:
    workflow_extra:
      env:
        MY_VAR: hello
    jobs:
      review:
        timeout-minutes: 240
        runs-on: ubuntu-22.04-large
```

YAML has a native `null` literal, so RFC 7396's null-deletes works
directly — e.g. drop the cron from a scheduled workflow while keeping
`workflow_dispatch`:

```yaml
workflows:
  nightly:
    workflow_extra:
      on:
        schedule: null
```

Workflow-level (`workflow_extra`) and job-level (`jobs.<name>`) overrides
are supported; step-level is not — the `setup:` mechanism handles step
injection. No allowlist of override keys; unknown job names produce a
warning.

When overrides are present, the generator renders the base template,
parses it, merges the overrides, and re-serializes. Output YAML formatting
differs slightly from the base template (block-style lists) but is
functionally identical.

## Auth

Each adopter creates a GitHub bot account and a classic PAT (`public_repo`
for public repos, `repo` for private) plus `workflow`, `notifications`,
`write:discussion`, `gist`, `user`. The PAT and a Claude OAuth token are
stored as secrets in the repo's `tend` GitHub Environment, whose deployment
branch policy admits only the branches `tend check` confirmed the bot
cannot write — the default branch and any `protected_branches` that exist
and are protected. A workflow the bot pushes to any other ref is refused
them before its first step. The `gist` scope supports bot-owned secret gists
used by `review-reviewers` as a per-month structured evidence store
(avoids the 65 KB comment-body limit). The `user` scope lets `install-tend`
set the bot's profile bio (`PATCH /user`) so the account's authorization
stance is discoverable on the bot's user page.

Classic PATs are all-or-nothing — `public_repo` grants full write to every
public repo the user can access. Fine-grained PATs allow per-category
scoping but don't support outside collaborators ([GitHub roadmap
#601](https://github.com/github/roadmap/issues/601), not shipped).

**Current privilege model: write + branch protection + environment gate.**
The bot has write access; a merge restriction (ruleset or branch
protection) is the primary security boundary — without it the bot can merge
its own PRs — and the `tend` environment keeps the operational secrets out
of any run the bot can cause on its own. `tend check` verifies both are
configured correctly, and `--fix` creates either. See
`docs/security-model.md` for the full threat model. Alternative models
(GitHub App, triage+fork) are in `TODO.md`.

The environment half is still being adopted: a repo is protected only once
its secrets are in the environment and the repo-level copies are deleted —
per-repo work `TODO.md` tracks. Until then it runs with its old exposure.

This repo's own workflows name it too, including the hand-maintained ones
the generator never touches.

## Concurrency and filtering

Events pass through three layers before the bot does work:

1. **GHA `if:` conditions** — evaluated by Actions before the job starts.
   A false condition skips the job entirely (never enters the concurrency
   group, never queues).
2. **Custom `should_run` logic** (mention only) — a lightweight verify job
   checks engagement before the expensive handle job runs.
3. **Concurrency groups** — at most one running job per group.

Concurrency groups:

| Workflow | Group key | Cancel-in-progress |
|---|---|---|
| review | `workflow-PR#` | yes — new push invalidates a review |
| mention/relay | none | stateless — secretless job that re-posts review events as a `repository_dispatch` |
| mention/verify | none | stateless |
| mention/handle | `workflow-handle-issue#\|PR#` | **no** — each mention runs to completion |
| triage | `workflow-issue#` | yes — latest comment wins |
| ci-fix / nightly / weekly | none | rare overlap or cron-serialized |

**Fork guard.** Workflows whose triggers can fire from a fork's own
Actions (`schedule`, `workflow_dispatch`, `workflow_run`, `issues`) carry
`if: github.repository_owner == '<owner>'` so a fork that's enabled
Actions but doesn't have the bot/Claude secrets no-ops cleanly. The
canonical owner is detected at `init` time (via `gh repo view`, walking
`source.owner.login` if the local repo is itself a fork) and pinned in
the generated workflow. `tend-review` uses `pull_request_target` (base
repo only) and `tend-mention`'s review-event paths already filter forks
via `head.repo.full_name == github.repository`, so neither needs the
guard.

**GHA queue depth = 1.** With `cancel-in-progress: false` (mention/handle),
when a third job arrives while one runs and one queues, the pending job is
replaced. Mitigation lives in the skill prompts: dedup if the bot already
responded to the triggering comment; self-heal earlier comments without a
bot reply (oldest first). The workflow injects the queue-to-run time delta
(seconds between event timestamp and job start) into the prompt — over
~40 s indicates the job was queued behind another run, making conversation
drift more likely.

## Skill design: bundled for everyone, overlay for one

Bundled skills in `plugins/tend-ci-runner/skills/` supply defaults. Consumer
repos overlay them at `.claude/skills/running-tend/SKILL.md`; where the two
conflict, the overlay wins.

When writing a bundled skill, keep the content universal — it applies to
every consumer. Repo-specific policy, taste, or convention (PR title
formats, label names, branch routing) belongs in an overlay. Tend has its
own overlay at `.claude/skills/running-tend/SKILL.md` — use it for guidance
that only applies to developing tend itself.

### Authoring skills

When adding to or editing files in `plugins/tend-ci-runner/skills/` or
`.claude/skills/`:

- **Be brief.** Skills are loaded into every relevant session — extra prose
  is overhead. Lead with the rule or recipe; cut motivation, anecdotes, and
  historical context unless required to apply the rule.
- **No specific past-run references.** Don't link GitHub Actions runs, cite
  session IDs, or quote durations from individual incidents. They age into
  trivia and aren't useful when the skill is reused. State the structural
  rule without the run links.
- **No specific past-case references.** Don't cite individual past PRs,
  issues, or commits as supporting precedent for a rule (e.g. "[#123] tried
  X and was closed"). They age into trivia, accumulate as the rule is
  re-tested, and create read-time work. State the structural rule (what
  shape is accepted, what shape is not) without naming the cases that
  produced it.
- **Date-stamp only when the value depends on time.** A baseline number
  expected to drift can be dated; a one-shot incident citation should not be.
- **Prefer recipe over narrative.** A code block plus a one-sentence framing
  beats a multi-paragraph explanation.
- **Examples over templates; open frames over closed menus.** For text the
  agent *produces* (comments, PR bodies, summaries), give an example labeled
  as such rather than verbatim wording to paste — canned phrasing reproduced
  literally reads awkwardly off-situation. For choices the agent *makes* (when
  to respond, which cases to check), frame the goal and give examples rather
  than a list it will read as exhaustive. Reserve mandatory exact wording for
  fragile mechanics: dedup keys, commands, API formats.

## Agent-driven vs deterministic steps

Tend's workflows invoke the agent through the harness-specific composite
action (`max-sixty/tend/claude@X.Y.Z` for Claude, `max-sixty/tend/codex@X.Y.Z`
for Codex). When adding new capability, split work along this line:

- **The agent drives diagnostics and remediation.** Once the action is
  running, put logic into the relevant skill (or a script the skill calls —
  see `plugins/tend-ci-runner/scripts/`). The agent handles edge cases,
  interprets output, and writes clearer messages than shell.
- **Actions gate whether the agent runs at all.** Agent invocations cost
  tokens; gating them in YAML is cheap. Pre-check steps that early-exit
  the job (e.g. `tend-notifications`'s "Check for unread notifications")
  save an entire agent run when there's nothing to do.

Don't build deterministic YAML steps for work that happens *inside* an
agent run. Extend the skill instead.

## Live testing against real GitHub

For live experiments against real GitHub behavior (environments, branch
protection, workflow triggers, secret release), `tend-agent/tend-integration`
is a persistent public repo we own, admin via the `tend-agent` account
(`gh auth token --user tend-agent`). Its `main` is branch-protected with
`enforce_admins: false`, so the owner can push directly and reset in place.
The weekly integration test (`.claude/skills/running-tend/references/integration-test.md`)
also drives it, so clean up any probe artifacts (extra workflows, branches,
environments, dummy secrets) when done.

## Site and worker

`site/` is the Astro marketing site (tend-src.com); `worker/` is the
Cloudflare Worker that serves its two data streams from `data/consumers.json`.

The site's dev server starts automatically per worktree via a `wt` post-start
hook (`.config/wt.toml`) on a deterministic port derived from the branch name.
Get the URL with `wt list statusline --format json | jq -r '.[].url'`; logs
land in `.git/wt/logs/`. Don't run `npm run dev`; it duplicates the running
server on a different port.
