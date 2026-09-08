# Security model (install-time reference)

The security decisions made during install and their rationale. This file
ships with the plugin so it resolves in any repo the skill runs in. The
canonical, full threat model is maintained in the tend source repo at
https://github.com/max-sixty/tend/blob/main/docs/security-model.md; this is
the subset an installing agent needs.

## The chain: merge authority is explicit

Tend runs an agent with write access on attacker-controlled input. The
boundary is structural and policy-dependent. Under `maintainer`, the bot cannot
update the default branch. Under `yolo`, it receives a pull-request-only
bypass, but direct pushes remain blocked and `.github/**` plus
`.config/tend.yaml` require a fresh CODEOWNER approval the bot cannot bypass.
The ownership block also protects every possible CODEOWNERS file and every
agent instruction path (`CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`,
`AGENTS.override.md`, `.claude/`, and `.agents/`). Its owner must be a maintainer GitHub user
distinct from the bot.
Preflight reads GitHub's `current_user_can_bypass` answer with the bot's own
token and requires the exact configured state: `never` or
`pull_requests_only`.

The two admin-gated operations are:

- **Updating the default branch.** `Merge access` protects creation, update,
  and deletion, with an admin bypass in both
  modes and, in yolo only, a bot-user `pull_request` bypass. `Control-plane
  review` layers fresh CODEOWNER approval over workflow and Tend-config paths.
- **Updating extra protected branches.** `Protected branch access` protects
  creation, update, and deletion and remains admin-only under both modes.
- **Operating on a tag.** A ruleset with the `creation` and `update`
  rules covering all tags (`~ALL` on a `tag`-target ruleset), admin-only
  bypass. Blocks the bot from pushing a new tag and from force-pushing
  (re-pointing) an existing one. `update` is required: force-pushing an
  existing tag maps to `update`, not `creation`, so without it a write-
  access actor could re-point an admin-pushed `v1.2.3` to a malicious
  commit. `deletion` is not in the chain: recreation is already blocked
  by `creation`, so a deleted tag can't be substituted with malicious
  code; the only damage is brief availability of the tag itself.

GitHub's immutable-releases setting closes the adjacent Releases API path:
once a release is published, its record, assets, and associated tag are
locked. The setting is prospective, so install-tend enables it before the
next release and `tend check` verifies it directly.

The "all tags" scope is deliberate: matching every tag removes a per-repo
pattern choice and keeps the chain a single uniform rule. Adopters that
need a narrower or layered configuration (per-pattern rulesets,
no-bypass immutability on release tags for repos that publish actions
consumed via tag pins, required-reviewer environment gates for per-deploy
maintainer approval) can layer additional rulesets and environment protection
rules on top; install-tend packages the simplest configuration that holds
the chain.

Secrets chain to the same refs through GitHub Environments: a job that
names an environment runs only if the run's ref matches the environment's
`deployment_branch_policy`, and only such jobs receive its secrets.

Tend's own operational secrets — the bot token and harness auth — live in
the `tend` environment (step 7 creates it), whose policy names only the
branches `tend check` confirmed for Tend's hardened runtime — the default
branch and any `protected_branches` that exist and are protected. Every generated secret-bearing job
names it, so a workflow the bot pushes to a branch is refused the secrets
before its first step: write access does not imply secret access.
Under yolo, `tend check` additionally requires the exact current generated
workflows on the default branch and refuses any other workflow that uses the
`tend` environment, names an environment dynamically, or calls an external or
ref-qualified reusable workflow whose environment use Tend cannot inspect. Yolo config
rejects runner-side setup and workflow/job overrides, so those generated jobs
retain the credential-isolating shape that justifies this exception.
Environment secrets overlay repo-level ones, and a job naming a
missing environment still runs, so an unfinished migration degrades to
repo-level exposure rather than breakage; `tend check` fails until the
policy is set, the secrets are in the environment, and the repo-level
copies are deleted.

Deploy and publish workflows declare their own Environments whose
policies list bot-inaccessible refs (under maintainer, the default branch and/or
all tags),
and their release secrets live there rather than at repo level. A leaked
bot token can push a non-default branch, but no ref it can push matches
such a policy, so the deploy job is rejected before it reads the secret:
no admin operation → no admin-gated ref → no environment access → no
secret. `tend check` sweeps every credential-holding environment — release
and operational alike — and fails on any it cannot confirm gated by a
non-bot reviewer or a policy of verified refs, so the chain is checked
rather than assumed. A credential is a stored secret or the OIDC token a
job requesting `id-token: write` mints in the environment's name, so a
trusted-publishing repo that stores nothing is swept the same way.

That holds only for a workflow whose sole path to invocation is updating a
bot-inaccessible ref (`push: tags:`, or under maintainer `push:` on the default
branch). A yolo default-branch deploy needs a non-bot environment reviewer.
Three triggers let a write-scoped bot supply the run's payload as well as
fire it, at a ref the policy already admits: `release: published`
(creating a release against an existing tag takes no tag operation),
`repository_dispatch`, and a `workflow_dispatch` carrying inputs. Those
need a required reviewer on the Environment, which holds regardless of
ref. A job requesting `id-token: write` outside any environment has no
gate at all — the token carries no environment claim, and the bot can
mint it from a branch it pushes. The canonical treatment, including which
triggers were probed rather than inferred, is the source repo's
`docs/security-model.md` linked above; install does not configure release
secrets.

The composite action refuses to start if the default branch does not match the
configured merge mode. Runner-side `setup` accepts shell steps only; actions
are refused because their deferred POST code can consume state the agent
controlled. After a run, the harness kills the sandbox and moves the
agent-owned checkout away before `actions/checkout` POST cleanup, leaving no
`.git/config` from which an agent-planted helper could execute.

Everything else (config pinning, rate limiting, fixed prompts) is defense
in depth.

## If a token leaks

| Token | Lifetime | If leaked, attacker can... | ...but cannot |
|-------|----------|----------------------------|---------------|
| Bot token (PAT) | Long-lived | Push to unprotected branches, create PRs, impersonate the bot; under yolo, merge ordinary PRs | Push directly to the default branch, alter yolo control-plane paths without CODEOWNER approval, operate on tags, or read environment secret values |
| Bot token (App) | ~1 hour | Same as PAT, until the token expires | Same, plus auto-expiry |
| Claude OAuth | Long-lived | Run Claude sessions billed to the account | Access GitHub |
| `OPENAI_API_KEY` | Until revoked | Run Codex/OpenAI calls billed to the account | Access GitHub |
| `CODEX_AUTH_JSON` | Current access-token lifetime | Run Codex against the ChatGPT account | Refresh itself or access GitHub |
| `CODEX_REFRESH_AUTH_JSON` | Rotating, effectively long-lived | Mint new ChatGPT access and refresh tokens | Access GitHub |
| `CODEX_REFRESH_PAT` | Until revoked | Rewrite secrets and configuration in this repo's `tend` environment | Read secret values or push code |

## Experimental Codex subscription auth

Sharing Codex's normal `auth.json` does not work: a consumer near access-token
expiry, or one recovering from a 401, can rotate the refresh token and leave
every other runner with invalid state. Tend instead stores two projections:

- `CODEX_AUTH_JSON` uses Codex's internal `chatgptAuthTokens` mode and has an
  empty refresh token. Every consumer may reuse its bearer token concurrently,
  but none can rotate the chain.
- `CODEX_REFRESH_AUTH_JSON` is the normal full `chatgpt` bundle. Only the
  serialized `tend-codex-auth-refresh` workflow reads it. Once OpenAI rotates
  the token, that workflow writes the full replacement first and the derived
  access-only bundle second.

`CODEX_REFRESH_PAT` is a fine-grained maintainer token scoped to this repository
with `Environments: write`; the workflow needs it because `GITHUB_TOKEN` cannot
rewrite Actions environment secrets. It is never passed to an agent session.

The consumer path is experimental and may break when OpenAI changes Codex
because it depends on an internal auth mode. The serialized weekly job runs
Codex's built-in refresh and persists its updated `auth.json`. Use a dedicated
ChatGPT account so the workflow's token rotation is independent of a
maintainer's local Codex login.

## Token assignment

Use a single bot token across all workflows for consistent identity. The
configured merge and control-plane rulesets cap its repository authority.

Two tokens are needed: the bot's PAT (or GitHub App) credential, plus a
harness-auth credential whose form depends on `harness` in
`.config/tend.yaml`.

| Token | Purpose |
|-------|---------|
| Bot token (PAT or App) | GitHub API and git operations. Consistent bot identity. |
| Harness auth (one of, per harness) | Authenticates the agent runtime. |
| ↳ Claude OAuth token | `harness: claude`: authenticates Claude Code to the Anthropic API. |
| ↳ Codex subscription trio | `harness: codex`: access-only consumer auth plus one weekly rotating writer (experimental; see above). |
| ↳ `OPENAI_API_KEY` | `harness: codex`: standard OpenAI API key, per-token billing. |

A single bot token is used across workflows because the same merge rules
bound all of them. One token also gives consistent bot identity for
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
