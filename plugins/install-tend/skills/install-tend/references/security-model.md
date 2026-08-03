# Security model (install-time reference)

The security decisions made during install and their rationale. This file
ships with the plugin so it resolves in any repo the skill runs in. The
canonical, full threat model is maintained in the tend source repo at
https://github.com/max-sixty/tend/blob/main/docs/security-model.md; this is
the subset an installing agent needs.

## The chain: every privileged path is admin-gated

Tend runs an agent with write access on attacker-controlled input. The
boundary is structural: every code path into a privileged workflow chains
back to an admin-controlled operation, and the bot has write, which is below
every role that can bypass a ruleset. The merge half is re-proven on every
run: preflight, holding the bot's own token, reads `current_user_can_bypass`
on each ruleset covering the default branch — GitHub's evaluation of the
bot's standing, teams and custom roles included — and aborts unless some
restrict-updates ruleset answers `never` or the branch is otherwise
protected.

The two admin-gated operations are:

- **Merging to the default branch.** A ruleset with the `update` rule on
  the default branch, admin-only bypass. Blocks the bot from landing code
  on the default branch.
- **Operating on a tag.** A ruleset with the `creation` and `update`
  rules covering all tags (`~ALL` on a `tag`-target ruleset), admin-only
  bypass. Blocks the bot from pushing a new tag and from force-pushing
  (re-pointing) an existing one. `update` is required: force-pushing an
  existing tag maps to `update`, not `creation`, so without it a write-
  access actor could re-point an admin-pushed `v1.2.3` to a malicious
  commit. `deletion` is not in the chain: recreation is already blocked
  by `creation`, so a deleted tag can't be substituted with malicious
  code; the only damage is brief availability of the tag itself.

The "all tags" scope is deliberate: matching every tag removes a per-repo
pattern choice and keeps the chain a single uniform rule. Adopters that
need a narrower or layered configuration (per-pattern rulesets,
no-bypass immutability on release tags for repos that publish actions
consumed via tag pins, required-reviewer environment gates for per-deploy
human approval) can layer additional rulesets and environment protection
rules on top; install-tend packages the simplest configuration that holds
the chain.

Secrets chain to the same refs through GitHub Environments: a job that
names an environment runs only if the run's ref matches the environment's
`deployment_branch_policy`, and only such jobs receive its secrets.

Tend's own operational secrets — the bot token and harness auth — live in
the `tend` environment (step 7 creates it), whose policy names only the
branches `tend check` confirmed the bot cannot write — the default branch
and any `protected_branches` that exist and are protected. Every generated secret-bearing job
names it, so a workflow the bot pushes to a branch is refused the secrets
before its first step: write access does not imply secret access.
Environment secrets overlay repo-level ones, and a job naming a
missing environment still runs, so an unfinished migration degrades to
repo-level exposure rather than breakage; `tend check` fails until the
policy is set, the secrets are in the environment, and the repo-level
copies are deleted.

Deploy and publish workflows declare their own Environments whose
policies list the admin-gated refs (the default branch and/or all tags),
and their release secrets live there rather than at repo level. A leaked
bot token can push a non-default branch, but no ref it can push matches
such a policy, so the deploy job is rejected before it reads the secret:
no admin operation → no admin-gated ref → no environment access → no
secret. `tend check` sweeps every secret-holding environment — release
and operational alike — and fails on any it cannot confirm gated by a
non-bot reviewer or a policy of verified refs, so the chain is checked
rather than assumed.

That holds only for a workflow whose sole path to invocation is updating
an admin-gated ref (`push: tags:`, or `push:` on the default branch).
Triggers a write-scoped bot can fire itself — `workflow_dispatch`,
`release: published`, `deployment`, `schedule`, chained dispatches — need
containment of their own, typically required reviewers on the
Environment. The canonical treatment, including which triggers were
probed rather than inferred, is the source repo's `docs/security-model.md`
linked above; install does not configure release secrets.

The composite action refuses to start if the default branch is unprotected.

Everything else (config pinning, rate limiting, fixed prompts) is defense
in depth.

## If a token leaks

| Token | Lifetime | If leaked, attacker can... | ...but cannot |
|-------|----------|----------------------------|---------------|
| Bot token (PAT) | Long-lived | Push to unprotected branches, create PRs, impersonate the bot, indefinitely | Merge PRs (merge restriction), push to the default branch, read any environment-gated secret — operational or release — from a workflow it pushes |
| Bot token (App) | ~1 hour | Same as PAT, until the token expires | Same, plus auto-expiry |
| Claude OAuth | Long-lived | Run Claude sessions billed to the account | Access GitHub |
| `OPENAI_API_KEY` | Until revoked | Run Codex/OpenAI calls billed to the account | Access GitHub |

## Codex auth.json is not supported

`harness: codex` accepts only `OPENAI_API_KEY`. The subscription
`auth.json` path is not exposed because Codex rotates that refresh
token on every API call and invalidates the prior one after a short
grace window. Tend runs multiple workflows concurrently
(review/mention/triage/nightly/…), so each in-flight job's call
invalidates the credential the other in-flight jobs are using — a
scheduled refresher works around the ~8-day full-rotation schedule
but cannot solve the per-call collision between concurrent jobs.
OpenAI's own
[CI/CD auth guide](https://developers.openai.com/codex/auth/ci-cd-auth)
forbids sharing one `auth.json` across concurrent jobs and
discourages it for public repos.

If `auth.json` was previously installed, replace it with an
`OPENAI_API_KEY` secret and delete the `CODEX_AUTH_JSON` and
`CODEX_REFRESH_PAT` secrets plus any `codex-auth-refresh.yaml`
workflow.

## Token assignment

Use a single bot token across all workflows for consistent identity. The
merge restriction caps blast radius regardless of which token is used.

Two tokens are needed: the bot's PAT (or GitHub App) credential, plus a
harness-auth credential whose form depends on `harness` in
`.config/tend.yaml`.

| Token | Purpose |
|-------|---------|
| Bot token (PAT or App) | GitHub API and git operations. Consistent bot identity. |
| Harness auth (one of, per harness) | Authenticates the agent runtime. |
| ↳ Claude OAuth token | `harness: claude`: authenticates Claude Code to the Anthropic API. |
| ↳ `OPENAI_API_KEY` | `harness: codex`: standard OpenAI API key, per-token billing. The subscription `auth.json` path is not supported (see above). |

A single bot token is safe across workflows because the merge restriction
caps the blast radius. One token also gives consistent bot identity for
reviews and comments and avoids the `github-actions[bot]` branding.

## Bot credential storage on the maintainer's machine

Install (step 8) keeps each bot's gh auth in a dedicated config dir,
`$HOME/.config/gh-bots/<bot-name>`, selected per command with
`GH_CONFIG_DIR`, with the token stored plaintext (mode 0600) in that
dir's `hosts.yml` via `--insecure-storage`. Two hazards drive this:

- **The OS keychain is shared.** gh keys keychain items by account name
  globally, not per config dir, so a keychain-backed bot login would
  share one item with the maintainer's default config. A device-flow
  code approved by the wrong github.com session, or a later
  `gh auth logout`, would then overwrite or delete the maintainer's own
  credential. With `--insecure-storage` nothing the bot dir does reaches
  the keychain; the dir can be deleted and rebuilt with no side effects.
- **git answers as the default config.** When gh is a git credential
  helper (`credential.helper = !gh auth git-credential`), a `git push`
  in a shell without an env token authenticates as the *default*
  config's active account. The bot never enters the default config, so a
  push can't land under its identity. This is also why 8b's login omits
  `--git-protocol https` — the flag writes gh's helper into the global
  git config, host-wide, since git config is not scoped by
  `GH_CONFIG_DIR` — and why bot tokens are scoped to single commands
  rather than exported: git's gh helper forwards an ambient env token
  too, as `x-access-token`.

The plaintext copy adds no exposure: the same token is already stored
server-side as an Actions secret, and the dir is readable only by the
maintainer's user. The dir is the bot's durable store, not install
scratch — scope audits and reinstalls read it to skip a fresh device
flow — so it outlives the install.

The empty-token guards in 8c/9/10 exist because gh treats a set-but-empty
`GH_TOKEN` as unset and silently falls back to stored credentials — the
maintainer's. An unguarded block after a failed token read would blank
the repo secret (`gh secret set` accepts an empty body), accept the
maintainer's invitations instead of the bot's, or overwrite the
maintainer's profile bio.

## How tokens flow through workflows

Two independent authentication paths exist in every workflow:

1. **Git CLI** (`git push`): authenticates with the token from
   `actions/checkout`. When no explicit token is passed it defaults to
   `GITHUB_TOKEN` scoped by the `permissions:` block; passing an explicit
   token swaps in that token's scopes.
2. **GitHub API** (`gh pr create`, `gh api`): `claude-code-action`
   overwrites the `GITHUB_TOKEN` env var with its `github_token` input.

All workflows should pass the bot token to both paths.

Bind the bot token to `GITHUB_TOKEN`, not `GH_TOKEN`. `GITHUB_TOKEN` is
auto-injected by GitHub Actions and read by most third-party tools;
overriding it gives one bot identity everywhere in the job. `GH_TOKEN`
only overrides the `gh` CLI; anything else still sees the auto-injected
`github-actions[bot]` token.
