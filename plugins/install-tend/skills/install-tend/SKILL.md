---
name: install-tend
description: Sets up tend — an autonomous junior maintainer for a GitHub repo, powered by Claude or OpenAI Codex — that reviews PRs, triages issues, and fixes CI. Creates config, generates workflows, configures secrets and branch protection via API, creates the bot account, and provisions the harness auth token (Claude OAuth or OpenAI API key). Use when setting up tend on a new repo or when asked to install/configure tend.
---

# Install Tend

Set up tend on the current repo. If the user hasn't supplied a bot name,
get one via `AskUserQuestion` before step 1 using the candidate-generation
pattern from step 6 (`<repo>-bot`, `<repo>-tend`, `tend-<repo>`, parallel
availability check, present available ones). The user can pick "Other"
to supply a custom name.

When asking the user questions during these steps, use the `AskUserQuestion`
tool — present concrete options when there are clear choices (e.g. bio
stance, badge style, secret-migration confirmation).

When a question requires the user to do something off-screen (visit a URL,
run a command, paste a value back), spell the next step out in the question
or option description: the exact web link, the exact command. "Generate a
token on the registry's site" is not enough — give the URL. The user should
not have to ask "where do I do that?".

## Kickoff

Before running step 1, choose the harness and lay out the plan:

- Ask via `AskUserQuestion` which harness to use:
  - **Claude (Anthropic)** — uses a Claude Code OAuth token (recommended
    for adopters with a Claude subscription) or a console.anthropic.com
    API key. OAuth draws from the subscription's usage limits; the API
    key bills per token, which fits when the user has no subscription to
    draw on or wants a dedicated billing surface and per-key revocation.
  - **Codex (OpenAI)** — uses an OpenAI API key (pay-per-token). The
    `auth.json` subscription path is incompatible with tend's
    concurrent workflows (per-call refresh-token invalidation) and is
    being removed. Detail in
    ${CLAUDE_SKILL_DIR}/references/security-model.md.
- List the steps you'll be running (the section headings below: Create
  config → Generate workflows → Branch protection → Skill overlay →
  Badge → Bot account → Harness auth → Bot token → Grant access →
  Bot bio → Commit) so the user knows what's coming.
- Tell them it typically takes 5–10 minutes of their hands-on time
  (browser logins, OAuth approvals, occasional copy-paste); the agent
  drives the rest.
- Confirm via `AskUserQuestion` ("Ready to start?") before beginning
  step 1. Don't proceed until they say yes.

Follow each step in order. Skip steps that are already done — check each
prerequisite before acting. Derive `REPO` once at the start:

```bash
gh auth status
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
echo "$REPO"
```

Confirm with the user that `$REPO` is the canonical repo where tend
will run — not a fork. Every command below passes `--repo "$REPO"`
explicitly, so if the working directory is a fork clone, just
override the variable with the canonical `owner/name` and continue;
no need to touch git remotes.

## Browser sessions

Step 6 (when the bot account must be created) and step 8's mint paths
(8a/8b) need a browser session logged in as the bot.
`mcp__claude-in-chrome__*` automation can drive both when available;
otherwise, give the user URLs and wait for confirmation. Before acting
as the bot, verify the logged-in user via the avatar menu.

## 1. Create config

Create `.config/tend.yaml` with at minimum `bot_name`, plus `harness` if
the user chose Codex (the default Claude harness can be omitted). See
README.md "Harnesses" for the comparison.

```yaml
bot_name: <bot-name>
# For Codex, also:
# harness: codex
# effort: medium   # optional: low | medium | high | xhigh
```

Check whether the repo already has a bot token secret under a non-default name:

```bash
gh secret list --repo "$REPO" --json name --jq '.[].name'
```

If a bot-token-like secret exists (e.g., `GH_BOT_TOKEN`, `ROBOT_PAT`),
suggest overriding the default name rather than creating a duplicate:

```yaml
secrets:
  bot_token: GH_BOT_TOKEN
```

Any repo-level secret not in `secrets.allowed` triggers a `tend check`
warning. Classify each non-bot secret and act now — don't defer:

- Build/observability tokens (e.g., `CODECOV_TOKEN`, `SENTRY_DSN`) are
  fine at the repo level. Add them to the allowlist:

  ```yaml
  secrets:
    allowed: ["CODECOV_TOKEN"]
  ```

- Release secrets (registry tokens like `PYPI_TOKEN`/`NPM_TOKEN`, signing
  keys, deploy credentials) at the repo level are reachable from any
  workflow run, including ones a write-access bot can trigger with no
  merge. Don't allowlist them. Migrate each to a GitHub Environment whose
  deployment policy pins to the admin-gated refs from §3 (the default
  branch and/or all tags). The bot can reach neither ref class, so it
  cannot reach the secret.

  Migrate the secret: recreate it on the Environment, delete the
  repo-level copy (confirm via `AskUserQuestion` first), and set
  `environment: <name>` on the publishing job.

  Configure the deployment policy. Allow whichever ref classes the
  workflow runs on:

  ```bash
  REPO=<owner>/<repo>; ENV=<name>
  DEFAULT_BRANCH=$(gh api "repos/$REPO" --jq .default_branch)
  gh api --method PUT "/repos/$REPO/environments/$ENV" \
    -F 'deployment_branch_policy[protected_branches]=false' \
    -F 'deployment_branch_policy[custom_branch_policies]=true'
  # Continuous-deploy on default branch:
  gh api --method POST "/repos/$REPO/environments/$ENV/deployment-branch-policies" \
    -f "name=$DEFAULT_BRANCH" -f type=branch
  # Release on tags (workflow has `on: push: tags:`):
  gh api --method POST "/repos/$REPO/environments/$ENV/deployment-branch-policies" \
    -f 'name=*' -f type=tag
  ```

  Verify:

  ```bash
  gh api "/repos/$REPO/environments/$ENV/deployment-branch-policies" \
    --jq '.branch_policies | map({name, type})'
  ```

  Each entry must match a ref class from §3 (default branch and/or all
  tags). Confirm before checking the box.

  Then sweep deploy/publish workflows. Each must trigger on `push: tags:`
  or `push: branches: [<default-branch>]` (per §3 workflow design) and
  declare an Environment. The grep below catches the common shapes; it
  misses reusable workflows in other repos and over-matches
  `pull_request_target` references in expressions and step inputs, so
  read each hit:

  ```bash
  grep -RniE 'tags:|workflow_dispatch|release:|schedule:|workflow_run|repository_dispatch|deployment:|pull_request_target' .github/workflows
  ```

  An OIDC-to-cloud deploy has no secret to migrate; the Environment with
  its admin-gated deployment policy plus the cloud provider's trust policy
  is then the only control on that path.

  The original repo-level secret value isn't readable (GitHub secrets are
  write-only), so a fresh token is needed. Ask the user via `AskUserQuestion`
  how to obtain it; recommend whichever fits the registry:

  - **CLI** — if the registry has a token-issuing CLI (e.g., `npm token create`),
    run it and capture the token.
  - **Chrome** — drive the registry's token page via `mcp__claude-in-chrome`
    (most registries — PyPI, crates.io, Docker Hub — only issue tokens via
    the web UI). Some registries (PyPI in particular) force a 2FA reauth
    at token-creation time; Chrome MCP can't drive that second factor.
    If the reauth prompt appears, fall back to Manual.
  - **Manual** — user generates the token themselves on the registry's
    site and pastes it back.

  Whichever route is chosen, include the exact token-creation URL in
  the question or option description (and in the follow-up message if
  manual). Common registries:

  - PyPI: `https://pypi.org/manage/account/token/`
  - npm: `https://www.npmjs.com/settings/<user>/tokens/new` (or `npm token create`)
  - crates.io: `https://crates.io/settings/tokens`
  - Docker Hub: `https://app.docker.com/settings/personal-access-tokens`
  - GitHub Packages / deploy: `https://github.com/settings/tokens`

  For other registries, look up the token page before asking. Accept any
  other route the user suggests. Never ask the user to dig the old token
  out of their password manager and re-paste it — issuing a fresh token
  and revoking the old one is part of the migration's point.

Discover existing CI workflows so tend-ci-fix can watch them:

```bash
grep -l 'push:\|pull_request' .github/workflows/*.yml .github/workflows/*.yaml 2>/dev/null
```

For each match, extract the workflow `name:` field. These are the workflows
that run tests, linting, or builds — tend-ci-fix should watch them. Configure:

```yaml
workflows:
  ci-fix:
    watched_workflows: ["ci", "lint"]  # names of workflows to watch
```

If no CI workflows exist, either skip ci-fix (`enabled: false`) or help the
user create one first.

Ask via `AskUserQuestion` (`multiSelect: true`) which other overrides
they want to set. Skip-all is fine — defaults are sensible:

- Setup steps (system deps, language version, pre-build hooks)
- Workflow conditions (e.g., skip review on `tend:dismissed` PRs — see below)
- Schedule overrides (cron timing for nightly/weekly)
- Permissions / timeouts on specific jobs
- Top-level env vars

For each selected category, follow up with a free-text ask, then write
the override into `.config/tend.yaml`. See the next subsection for
override syntax.

### Customizing generated workflow YAML

The generator owns every `tend-*.yaml` file — direct edits are lost on the next
`uvx tend@latest init`. Instead, set `workflow_extra` (top-level) or
`jobs.<name>` (job-level) overrides in `.config/tend.yaml`. Overrides follow
RFC 7396 (JSON Merge Patch): mappings deep-merge, scalars and lists replace.

Common example — skip review on PRs labeled `tend:dismissed` (so authors can
opt out of re-reviews after the initial pass). Because scalars replace under
Merge Patch, the override must duplicate the default draft check:

```yaml
workflows:
  review:
    jobs:
      review:
        if: "github.event.pull_request.draft == false && !contains(github.event.pull_request.labels.*.name, 'tend:dismissed')"
```

See ${CLAUDE_SKILL_DIR}/references/tend.example.yaml for more override
examples (extending permissions, timeouts, top-level env vars).

## 2. Generate workflows

```bash
uvx tend@latest init --with-install-test
```

`--with-install-test` adds a one-shot `tend-install-test.yaml` workflow
that runs on the install PR to verify secrets are set and the committed
workflows match the generator's current output. The next nightly regen
runs `uvx tend@latest init` without the flag, and the init cleanup step
removes the file from the default branch.

Verify workflow files appear in `.github/workflows/tend-*.yaml`. Run
`uvx tend@latest check` to validate branch protection, secrets, and bot access.

Check for workflows using `anthropics/claude-code-action`:

```bash
grep -rl 'anthropics/claude-code-action' .github/workflows/ 2>/dev/null
```

If found, delete them — tend replaces claude-code-action entirely. Remind the
user that team members should @-mention the bot account instead of `@claude`.

## 3. Ref protection

Two ref classes can land code that reaches a deploy or publish workflow:
the default branch (via merge) and tags (via tag push). Restrict both to
admin-only operations so every privileged code path chains back to an
admin action. The bot has write, which is below every role that can
bypass, so it satisfies neither.

Survey existing rulesets; skip any slot already covered:

```bash
gh api "repos/$REPO/rulesets" --jq '.[] | {name, target, enforcement}'
```

**Merge restriction on the default branch.** Create if missing:

```bash
gh api "repos/$REPO/rulesets" --method POST --input - << 'EOF'
{
  "name": "Merge access",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] }
  },
  "rules": [{ "type": "update" }],
  "bypass_actors": [{
    "actor_id": 5,
    "actor_type": "RepositoryRole",
    "bypass_mode": "exempt"
  }]
}
EOF
```

`actor_id: 5` is the admin role. The base role IDs run maintain 2, write 4,
admin 5 — not ordered by privilege, so the plausible guess for maintain is in
fact write, the bot's own role, and granting it hands the bot the merge. Before
adding any bypass actor, read back what the ruleset actually granted:

```bash
gh api graphql -f query='{repository(owner:"OWNER", name:"REPO")
  {rulesets(first:10){nodes{name bypassActors(first:10)
  {nodes{repositoryRoleDatabaseId repositoryRoleName}}}}}}'
```

**Tag operations.** Same shape, applied to all tags. Pushing a new tag or
moving an existing one becomes an admin operation; the bot can do
neither. Skipping the "what pattern do your tags use?" question is
deliberate: matching all tags removes a per-repo configuration choice
and gives the chain a single, uniform rule.

```bash
gh api "repos/$REPO/rulesets" --method POST --input - << 'EOF'
{
  "name": "Tag operations",
  "target": "tag",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["~ALL"], "exclude": [] }
  },
  "rules": [
    { "type": "creation" },
    { "type": "update" }
  ],
  "bypass_actors": [{
    "actor_id": 5,
    "actor_type": "RepositoryRole",
    "bypass_mode": "exempt"
  }]
}
EOF
```

`creation` blocks the bot from pushing a fresh admin-gated tag; `update`
blocks rewriting an existing tag to point at a bot-controlled commit. The
chain doesn't need `deletion` separately. Recreation is already blocked
by `creation`, so a deleted tag can't be replaced with malicious code.
Bot-deleting an admin-pushed tag is brief availability damage at worst;
repos that need stronger protection against published-tag deletion can
add a no-bypass `deletion` ruleset (see the publisher uplift below).

**Environment ref policies.** A ruleset only helps once an Environment
names the refs it protects. A new Environment defaults to
`deployment_branch_policy: null`, which admits every ref — so a bot-pushed
branch or tag reaches its secrets and mints its OIDC token. Survey what
exists, including environments GitHub created on the repo's behalf
(`github-pages`) and ones that predate tend:

```bash
gh api "repos/$REPO/environments" \
  --jq '.environments[] | {name, deployment_branch_policy, rules: [.protection_rules[].type]}'
```

Pin each environment that holds a secret, or that a job with
`id-token: write` names, to the admin-gated refs its workflows actually
use — all tags for a release, the default branch for a continuous deploy:

```bash
gh api "repos/$REPO/environments/$ENV" --method PUT --input - << 'EOF'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
gh api "repos/$REPO/environments/$ENV/deployment-branch-policies" \
  --method POST -f name='*' -f type=tag
```

`type` is `tag` or `branch` and defaults to `branch` when omitted, so a
tag entry that leaves it out silently protects nothing. Prefer this
explicit list to the "protected branches only" setting, which admits any
branch carrying a *classic* protection rule — a set that grows as
branches are created, and that excludes a branch protected by the
ruleset above.

**Release/deploy workflow design.** Workflows that use release or deploy
secrets must trigger on `push: tags:` (release) or `push: branches: [main]`
(continuous deploy from the default branch), and reference an Environment
(§1). Don't trigger on `pull_request`. A `pull_request` workflow runs the
YAML at the PR's head ref, which a bot can write, so the workflow code is
no longer admin-vetted and the chain breaks at the workflow file itself.
Triggers a write-scoped bot can fire *and* steer are outside the packaged
recipe: `release: published` (creating a release against an existing tag
takes no tag operation, and its body and assets are the bot's),
`repository_dispatch`, and a `workflow_dispatch` carrying inputs. Their
workflow files still run from the default branch, so the code is
admin-vetted, but the bot chooses when they fire and what payload they
see. If a repo keeps one on a release/deploy workflow, gate that
Environment with required reviewers before migrating release or deploy
secrets to it.

Run `uvx tend@latest check` after this section; its `release-protection`
line reports any environment still reachable by the bot.

**More complicated approaches are possible** (per-pattern tag rulesets,
mixed bypass actors, layered no-bypass immutability rulesets for repos
that publish actions consumed via tag pins, required-reviewer environment
gates for per-deploy human approval). Install-tend packages the recipe
above because it is the simplest configuration that holds the chain;
adopters with stricter requirements can layer additional rulesets or
environment protection rules on top.

## 4. Create skill overlay (recommended)

Create `.claude/skills/running-tend/SKILL.md` with tend-specific project
guidance. This skill is loaded by tend workflows alongside the generic
`tend-*` skills.

**Do NOT duplicate CLAUDE.md** and **do NOT invent project conventions.**

Ask via `AskUserQuestion` (`multiSelect: true`) which tend-specific
preferences they want to capture. Skipping all is fine — the placeholder
below covers that case.

- PR title format (e.g., conventional commits, Jira ticket prefix)
- Labels the bot should apply to its PRs
- Review request routing (specific teams or people)
- Target branch if not the default branch
- Optional nightly actions (e.g., changelog maintenance — specify file and branch)

For each selected item, follow up with a free-text ask to capture the
specifics, then write them into the overlay. If nothing is selected,
create a placeholder:

```markdown
No project-specific tend preferences yet. Add guidance here as
needed — this file is loaded by tend workflows alongside CLAUDE.md.
```

Build commands, test commands, code style, and project structure belong
in CLAUDE.md — tend reads it like any other Claude session.

## 5. Offer to add a badge

If the repo has a README (any of `README.md`, `README.rst`, `README`), offer
to add a "maintained with tend" badge.

Base URL (always include the logo):

```
https://img.shields.io/badge/maintained_with-tend-bba580?logo=data:image/svg%2bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxNiAxNiI+PGcgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMCwxNikgc2NhbGUoMC4wMTI1LC0wLjAxMjUpIiBmaWxsPSIjZmZmIiBzdHJva2U9Im5vbmUiPjxwYXRoIGQ9Ik02ODAgMTEyOCBjNjIgLTk2IDY5IC0xNzggMjAgLTI0MSAtMTcgLTIyIC0yMCAtNDAgLTIwIC0xMzQgbDEgLTEwOCAyMSAyOCBjMTEgMTYgMzAgNDcgNDIgNzAgMTIgMjIgMzIgNDkgNDYgNTkgMzcgMjcgMTE0IDM4IDE4NCAyNyA5MyAtMTUgOTQgLTE4IDQ0IC03OSAtNzIgLTg4IC0xMDkgLTExMyAtMTc2IC0xMTcgLTMxIC0yIC02NCAxIC03MiA2IC0yMyAxNSAyMSA1NiAxMDcgOTggNDAgMjAgNzEgMzggNjkgNDAgLTYgNyAtODggLTE3IC0xMjYgLTM3IC00OSAtMjUgLTEwMCAtNzggLTEyMSAtMTI1IC0xNSAtMzMgLTE5IC02NiAtMTkgLTE4OCAwIC0xNTcgOCAtMTk1IDUwIC0yMzIgMTcgLTE2IDM2IC0yMCA4NSAtMTkgNjIgMSA2MyAxIDczIC0zMiA5IC0zMiA5IC0zMyAtMjIgLTQwIC01MCAtMTIgLTEzMiAtNyAtMTY0IDEwIC00MCAyMSAtNzkgNjkgLTkyIDExNCAtNSAyMCAtMTAgMTAyIC0xMCAxODIgMCA4MCAtNSAxNjIgLTExIDE4NCAtMjIgNzkgLTEzNSAxNjYgLTIzNCAxODEgLTM3IDYgLTM1IDMgMzAgLTI4IDc4IC0zOSAxNDQgLTkxIDEzMiAtMTA0IC01IC00IC0zNyAtOCAtNzEgLTggLTc3IDAgLTExNyAyNCAtMTgyIDEwOSAtNTIgNjggLTUxIDcwIDQyIDg1IDcxIDExIDE0MyAwIDE4MyAtMjkgMTYgLTExIDQwIC00MyA1NCAtNzMgMTMgLTI5IDMyIC01OSA0MSAtNjYgMTQgLTEyIDE2IC03IDE2IDU4IDAgNTkgNCA3NyAyMyAxMDIgMTkgMjYgMjMgNDYgMjUgMTMwIDMgNjcgMCA5OSAtNyA5OSAtNyAwIC0xMSAtMjMgLTEyIC01NyAwIC0zMiAtNiAtNzYgLTEyIC05NyBsLTEyIC00MCAtMjcgMzIgYy0zNCA0MSAtNDMgOTYgLTI0IDE1MSAxNCA0MSA3NSAxNDEgODYgMTQxIDMgMCAyMSAtMjQgNDAgLTUyeiIvPjwvZz48L3N2Zz4K
```

Match the `style` parameter used by existing badges in the README. For
example, if the repo uses `style=for-the-badge`, append
`&style=for-the-badge` to the URL. If no existing badges or no style
parameter, use the default (no style parameter needed).

Wrap the image in a link to `https://github.com/max-sixty/tend` — always
this exact URL, regardless of the consumer's org or repo name. The full
markdown shape:

```
[![maintained with tend](<image-url>)](https://github.com/max-sixty/tend)
```

Use `AskUserQuestion` to confirm. Describe the badge briefly in the
question ("an olive-green 'maintained with tend' badge with the tend
wordmark") — do NOT paste the raw `img.shields.io` URL or its base64
logo blob into the chat; the blob is hundreds of characters of noise.
The user only needs to decide yes/no, not eyeball the URL. Insert the
markdown directly into the README on confirmation.

Place it near the top of the README — after the title/heading but
before the first paragraph. If there are already badges on that line,
append to the same line.

If no README exists, skip this step.

## 6. Bot account

```bash
gh api users/<bot-name> --jq '.login,.id' 2>/dev/null && echo "EXISTS" || echo "NOT FOUND"
```

If the account doesn't exist:

1. If the user hasn't chosen a name yet, generate three candidates
   (e.g. `<repo>-bot`, `<repo>-tend`, `tend-<repo>`), check availability
   in parallel, and present the available ones via `AskUserQuestion`:

   ```bash
   for name in cand1 cand2 cand3; do
     gh api "users/$name" >/dev/null 2>&1 && echo "$name: TAKEN" || echo "$name: available"
   done
   ```
2. Navigate Chrome to `https://github.com/signup`.
3. If a verification code is needed and an email-reading skill or MCP is
   available, use it to fetch the latest GitHub verification email
   (`from:github subject:code`); otherwise have the user paste the code.
4. After confirmation, re-verify via API.

## 7. Harness auth token

Branch on the harness chosen in Kickoff.

### 7a. Harness = claude

The Claude action accepts two auth modes; pick whichever the user has.
The action prefers `CLAUDE_CODE_OAUTH_TOKEN` when both are set.

```bash
gh secret list --repo "$REPO" --json name --jq '.[].name' \
  | grep -E -q '^(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY)$' \
  && echo "SET" || echo "NOT SET"
```

If not set, ask via `AskUserQuestion` which auth mode to use:

- **OAuth token (recommended for Claude subscribers)** —
  `sk-ant-oat01-…` from `claude setup-token`. Draws from the
  subscription's usage limits. Token is advertised as 1-year.
- **API key** — `sk-ant-…` from
  `https://console.anthropic.com/settings/keys`. Billed per token against
  the Console org. Pick this when there's no Claude subscription, when
  the bot should bill against a dedicated Console org, or when per-key
  revocation matters. Works for any repo.

For **OAuth token**: before offering the CLI option, check:

- `command -v claude` — if missing, only offer Manual (point them at
  `https://claude.com/claude-code` to install).
- `uname` — the bundled wrapper depends on `bash` + `script(1)` and
  has only been validated on macOS and Linux. On anything else
  (`MINGW*`, `CYGWIN*`, `MSYS*`, `Windows_NT`, etc.), only offer Manual.

- **CLI (recommended on macOS/Linux when `claude` is on PATH)** — run
  the bundled wrapper, which invokes `claude setup-token` (OAuth 2.0
  PKCE, opens browser):

  ```bash
  TOKEN=$("${CLAUDE_SKILL_DIR}/scripts/oauth-token.sh")
  ```

- **Manual** — have the user run `claude setup-token` in their own
  terminal (any machine with Claude Code installed) and paste the
  `sk-ant-oat01-…` token back. Use this on Windows or when the
  wrapper errors out.

Then store the secret:

```bash
echo "$TOKEN" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "$REPO"
```

For **API key**:

Have the user paste the `sk-ant-…` key, then store it:

```bash
gh secret set ANTHROPIC_API_KEY --repo "$REPO" --body "$KEY"
```

### 7b. Harness = codex

Codex uses `OPENAI_API_KEY` (pay-per-token, from
`https://platform.openai.com/api-keys`). The subscription `auth.json`
path is not supported — Codex rotates that refresh token on every
API call and invalidates the prior one, so tend's concurrent
workflows (review/mention/triage/nightly/…) would break each other's
auth mid-run. See ${CLAUDE_SKILL_DIR}/references/security-model.md.

```bash
gh secret list --repo "$REPO" --json name --jq '.[].name' | grep -q OPENAI_API_KEY && echo "SET" || echo "NOT SET"
```

If not set, have the user paste the `sk-…` key. Store it:

```bash
gh secret set OPENAI_API_KEY --repo "$REPO" --body "$KEY"
```

## 8. Bot token and secret

The bot's token needs scopes `repo`, `workflow`, `notifications`,
`write:discussion`, `gist`, and `user` (per-scope justifications in
${CLAUDE_SKILL_DIR}/references/tend.example.yaml).

This step checks what gh already stores for the bot, mints a token
only if needed (8a or 8b), and pushes it to the repo secret (8c). It
serves both the install sequence and a standalone `Bot PAT`
scope-audit remediation; in the audit case it is the whole fix, and
you close the issue once 8c verifies. `<bot-name>` is `bot_name` in
`.config/tend.yaml`; `$REPO` derives as in the kickoff:
`gh repo view --json nameWithOwner --jq '.nameWithOwner'` (confirm it
names the canonical repo, not a fork).

Bot auth lives in a dedicated config dir,
`$HOME/.config/gh-bots/<bot-name>`, with the token stored plaintext
(mode 0600) via `--insecure-storage` — never the OS keychain, which gh
keys by account name globally, so a keychain-backed bot login could
overwrite the maintainer's own credential. The bot also never enters
the default config, which git's gh credential helper answers as, so a
stray `git push` can't land as the bot. The token is already a repo
secret, so the on-disk copy adds no exposure. The dir is durable:
scope audits and reinstalls read it to skip a fresh device flow. Full
rationale: ${CLAUDE_SKILL_DIR}/references/security-model.md.

Three auth postures, one per command — never export a token for the
session (git's gh helper would forward it):

- **Bot dir** (`gh auth …`, `gh api user`): prefix with
  `env -u GH_TOKEN -u GITHUB_TOKEN GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>"`.
  Ambient env tokens otherwise hijack the reads and block the auth
  writes.
- **Bot via token** (`GH_TOKEN=$BOT_GH_TOKEN gh api …`): each block
  reads the token, then skips its action if the read came back empty —
  an empty `GH_TOKEN` silently falls back to the maintainer's stored
  auth, and `gh secret set` accepts an empty body. Guards skip rather
  than `exit` because blocks may be pasted into the user's shell.
- **Maintainer** (`gh secret`, the collaborator API in step 9): bare,
  on ambient auth. A 403 means that auth lacks admin — often a weak
  env token; `env -u` it to fall back to the stored login.

No step writes the maintainer's default config — and the bot must not
sit there either (pre-dir installs put it there). If a bare
`gh auth token --user <bot-name>` prints a token, evict it with
`env -u GH_TOKEN -u GITHUB_TOKEN gh auth logout --user <bot-name>`;
workflows run on the repo secret, so nothing breaks.

Check what the bot dir holds:

```bash
env -u GH_TOKEN -u GITHUB_TOKEN \
  GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>" gh auth status 2>&1
```

A missing dir prints "not logged in", which is a routing answer, not an
error to debug. Read the output:

- Logged in as `<bot-name>` with a `Token scopes:` line listing all six
  scopes → skip to 8c.
- Logged in as `<bot-name>`, scopes missing → **refresh path** (8a).
- Not logged in here → **login path** (8b).

8a and 8b both run gh's device flow: the command prints a one-time code
and polls until it is approved at `https://github.com/login/device` by
a browser logged in as the bot (codes expire after ~15 minutes). Run
the command yourself in the background, surfacing the code and URL;
delegate to the user's terminal only if that fails, and then hand over
the rest of the step's commands (8c, steps 9 and 10) fully
substituted — the token lives on whichever machine ran the login. On
Windows, run everything in Git Bash (bundled with Git for Windows); the
snippets work unmodified.

### 8a. Refresh path (bot already in the bot dir)

```bash
env -u GH_TOKEN -u GITHUB_TOKEN \
  GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>" \
  gh auth refresh --hostname github.com --insecure-storage \
  --scopes repo,workflow,notifications,write:discussion,gist,user
```

`gh auth refresh` has no `--user` flag; it operates on the dir's active
account, the bot. Requested scopes merge with the stored token's while
it is still valid (a revoked one yields just the six). No identity
check is needed: a wrong-session approval makes refresh itself error
("received credentials for <other-user>") — have the user re-approve
from the bot's session and rerun.

### 8b. Login path (first-time setup)

```bash
env -u GH_TOKEN -u GITHUB_TOKEN \
  GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>" \
  gh auth login --hostname github.com --web \
  --insecure-storage \
  --scopes repo,workflow,notifications,write:discussion,gist,user
```

No `--git-protocol` here: that flag writes gh's credential helper into
the global git config, host-wide (git config is not scoped by
`GH_CONFIG_DIR`).

`gh auth login` stores whatever account approved the code, without
checking. Verify before continuing:

```bash
env -u GH_TOKEN -u GITHUB_TOKEN \
  GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>" gh api user --jq '.login'
```

This must print the bot name. Anything else means the wrong session
approved the code; the token never left the bot dir, so delete the dir
and rerun 8b before proceeding — no other account is affected:

```bash
rm -rf "$HOME/.config/gh-bots/<bot-name>"
```

### 8c. Push token to secret

Copy the bot's token to the repo secret (`<secret-name>` is the
`secrets.bot_token` value from §1, default `TEND_BOT_TOKEN`; trust
this over any name an audit issue quotes) and verify the `Updated`
timestamp is fresh:

```bash
BOT_GH_TOKEN=$(env -u GH_TOKEN -u GITHUB_TOKEN \
  GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>" gh auth token --user <bot-name>)
if [ -z "$BOT_GH_TOKEN" ]; then
  echo "bot token empty — fix step 8 first" >&2
else
  gh secret set <secret-name> --repo "$REPO" --body "$BOT_GH_TOKEN"
  gh secret list --repo "$REPO"
fi
```

## 9. Grant bot access

The collaborator PUT and final list run as the maintainer (they need
admin); accepting the invitation runs as the bot. GitHub may grant
access directly (204) without creating an invitation — accept only if
one exists.

```bash
BOT_GH_TOKEN=$(env -u GH_TOKEN -u GITHUB_TOKEN \
  GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>" gh auth token --user <bot-name>)
if [ -z "$BOT_GH_TOKEN" ]; then
  echo "bot token empty — fix step 8 first" >&2
else
  gh api "repos/$REPO/collaborators/<bot-name>" -X PUT -f permission=push
  INVITE_ID=$(GH_TOKEN=$BOT_GH_TOKEN gh api "user/repository_invitations" --jq ".[] | select(.repository.full_name == \"$REPO\") | .id")
  if [ -n "$INVITE_ID" ]; then
    GH_TOKEN=$BOT_GH_TOKEN gh api "user/repository_invitations/$INVITE_ID" -X PATCH
  fi
  gh api "repos/$REPO/collaborators" --jq '.[].login'
fi
```

## 10. Bot profile bio

Capture what the creator is comfortable with contributors/users asking the
bot to do, then reflect that stance in the bot's profile bio (≤160 chars)
so it's discoverable on the bot's user page. This is advisory — the bot
doesn't gate behavior on it.

Ask the creator via `AskUserQuestion` which stance applies. Substitute
`<owner>/<repo>` at ask time. Order options recommended-first and mark
the recommended one explicitly:

- `tend agent for <owner>/<repo>. I triage issues and help maintain <repo>.` (Recommended — invites issue/PR engagement without inviting open-ended Q&A)
- `tend agent for <owner>/<repo>. Feel free to ask me questions about <repo>.` (Most permissive — invites contributor questions)
- `tend agent for <owner>/<repo>. I respond to maintainers of <repo>.` (Most restrictive — limits engagement to maintainers)

Check the current bio as the bot — skip the write if it already matches:

```bash
BOT_GH_TOKEN=$(env -u GH_TOKEN -u GITHUB_TOKEN \
  GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>" gh auth token --user <bot-name>)
if [ -z "$BOT_GH_TOKEN" ]; then
  echo "bot token empty — fix step 8 first" >&2
else
  GH_TOKEN=$BOT_GH_TOKEN gh api user --jq '.bio'
fi
```

Otherwise write it (requires `user` scope on the bot's token from step 8):

```bash
BOT_GH_TOKEN=$(env -u GH_TOKEN -u GITHUB_TOKEN \
  GH_CONFIG_DIR="$HOME/.config/gh-bots/<bot-name>" gh auth token --user <bot-name>)
if [ -z "$BOT_GH_TOKEN" ]; then
  echo "bot token empty — fix step 8 first" >&2
else
  GH_TOKEN=$BOT_GH_TOKEN gh api user -X PATCH -f bio="<drafted bio>"
fi
```

## 11. Commit and push

Stage all changes:

```bash
git add .
```

Commit with co-author attribution. Do NOT push without explicit permission.

After pushing the install PR, wait for the `tend-install-test` workflow
to pass before merging — it verifies the bot+harness secrets are set and
that the committed workflow files match the generator's output. The file
itself is removed on the next nightly regen, so future PRs won't trigger
it.

## Summary checklist

After completing all steps, present this checklist (harness-specific
line picks the row that matches the chosen harness):

- [ ] Config: `.config/tend.yaml` created (with `harness` set if Codex)
- [ ] Workflows: generated in `.github/workflows/`
- [ ] Rulesets: merge restriction on default branch (admin bypass), tag operations on all tags (admin bypass)
- [ ] Release/deploy secrets: environment-protected; the environment's deployment-branch-policies list only the admin-gated refs from §3 (default branch and/or all tags)
- [ ] Skill overlay: `.claude/skills/running-tend/SKILL.md` (tend-specific only)
- [ ] Badge: offered to add to README (optional)
- [ ] Bot account: `<bot-name>` exists on GitHub
- [ ] Harness auth (claude): `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` secret set
- [ ] Harness auth (codex): `OPENAI_API_KEY` secret set
- [ ] Bot token: the `secrets.bot_token` secret (default `TEND_BOT_TOKEN`) set with `repo`+`workflow`+`notifications`+`write:discussion`+`gist`+`user` scopes
- [ ] Bot access: repo collaborator with write access, invitation accepted
- [ ] Bot bio: profile bio reflects the authorization stance
- [ ] Committed (push requires explicit permission)
- [ ] `tend-install-test` workflow passed on the install PR before merging
