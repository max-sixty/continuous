# CI Automation Security Model

Tend gives an AI agent write access to a repository and runs it on
attacker-controlled input (PR diffs, issue bodies, comments, CI logs). The
agent needs enough access to be useful (push commits, post reviews, create
PRs) but every capability is a capability an attacker inherits if they can
hijack the session.

A determined attacker with time and skill will eventually get the tokens —
they're in memory during every workflow run, and Claude executes arbitrary
code. The goal isn't to make exfiltration impossible. It's to make the
tokens less valuable when leaked, limit what a hijacked session can do, and
make unsophisticated attacks fail outright.

Each adopting repo should document its specific configuration (admin accounts,
token names, protected environments) in its own `.github/CLAUDE.md`.

## Threats

Three things an attacker wants, roughly in order of severity:

1. **Merge malicious code to the default branch.** Game over — the attacker
   controls the repo. Everything else is damage limitation compared to this.

2. **Exfiltrate tokens.** The bot token grants write access to the repo
   (branches, PRs, comments). The Claude OAuth token grants billed API access.
   With a long-lived PAT, the attacker keeps access indefinitely.

3. **Hijack a single session.** Even without stealing tokens, an attacker who
   controls what Claude does in one run can push malicious branches, post
   misleading reviews, or create spam PRs.

The attack surface varies by workflow. `tend-review` is the most exposed —
the attacker controls the entire PR diff, which Claude reads and reasons
about. `tend-weekly` is the least exposed — triggered on a cron with no
user-controlled input.

| Workflow | Injection surface | Attacker control | Mitigations |
|----------|-------------------|-------------------|-------------|
| **review** | PR diff content, review body on bot PRs | Full (any PR) / Medium (reviewers) | Fixed prompt, merge restriction, CLAUDE.md pinning (fork PRs) |
| **triage** | Issue body | Partial (structured skill) | Fixed prompt, merge restriction, environment protection |
| **mention** | Comment body on any issue/PR | Full | Fixed prompt, merge restriction, engagement verification |
| **ci-fix** | Failed CI logs | Minimal (must break CI on default branch) | Fixed prompt, automatic trigger |
| **weekly** | None | None | Fixed prompt, scheduled trigger |

## What we do

There are two load-bearing boundaries, one per path code can take.

**Merge restriction** covers code that reaches the default branch through a
merge. A GitHub ruleset (or branch protection) prevents the bot from merging
to protected branches (the default branch plus any in `protected_branches`)
regardless of review status. The composite action's preflight verifies this
as the bot itself: `current_user_can_bypass` on each applying ruleset is
GitHub's own evaluation of the bot's standing — teams, custom roles, and
org-level rulesets included — and the run aborts if the bot can bypass every
restrict-updates ruleset, or if the branch is unprotected entirely.

**Environment-protected secrets** (below) covers code that runs *without* a
merge: a tag push, a release, a manual or chained dispatch. The merge
restriction does nothing there, so that gate carries the path on its own.

Everything else in this section is defense in depth: useful, but not
load-bearing.

**Action distribution integrity.** Generated workflows pin the composite
action to the generator's own release version
(`max-sixty/tend/<harness>@X.Y.Z`), never a floating ref. Release-tag immutability is the boundary this relies on: a
`tag` ruleset on `max-sixty/tend` restricts `update` and `deletion` (leaving
`creation` open so the release can push a new `X.Y.Z`), with no bypass for
write-access actors. That ruleset is applied out of band; until it is in
place the boundary holds by convention, not enforcement, and anyone with
write access on `max-sixty/tend` can rewrite a published tag. Once enforced,
a leaked bot token or hijacked session cannot move a published tag and
retroactively change the code every adopter already runs; the worst it can
do is push a new release tag, which adopters only pick up on their next
nightly regen, as a reviewable workflow-file diff in their own repo. Adopters extend trust to `max-sixty/tend`'s release-tag integrity the
same way they trust any third-party action's publisher; pinning to `X.Y.Z`
(or a commit SHA) bounds that trust to a reviewed, immutable point.

**Config pinning.** The Claude harness actions restore RCE-relevant config from
the PR base branch before the agent starts: `.claude/`, `.mcp.json`, `.claude.json`,
`.gitmodules`, `.ripgreprc`, `.husky`, plus `CLAUDE.md`/`CLAUDE.local.md` as a
prompt-injection defense. A malicious PR's `SessionStart` hook, MCP server, or
injected `CLAUDE.md` is reverted before Claude reads it. The restoration runs
in shell; the path list and ordering mirror claude-code-action's
`restore-config.ts`. The PR-authored versions of those paths are snapshotted to
`.claude-pr/` (added to `.git/info/exclude` so they're not tracked) before being
overwritten, so review skills can optionally inspect what the PR changed without
those files ever being executed.

**Setup runs on reviewed code.** Adopter `setup:` steps execute as the runner
user, which holds sudo and, until the sandbox setup strips it, the checkout
PAT in `.git/config`. So every generated workflow checks out reviewed code
before running them — the default branch, or in `tend-review` the PR's base
branch — and lands the PR's tree only afterwards. A contributor's build
backend, added dependencies, and local `uses: ./` actions therefore execute
under the agent, inside the sandbox, rather than ahead of it. `sandbox_setup:`
is the lever for project setup that must see the PR's own manifests. Codex
adopters get the ordering without the containment, since that harness runs the
agent on the runner.

**Credential isolation (Claude harnesses).** The Claude harness actions run the
agent as a separate non-sudo `tend-sandbox` user, sharing the proxy machinery
under the top-level `proxy/`. Both the bot PAT and the Anthropic credential (OAuth token
or API key) live only in a local mitmproxy that the agent reaches over
`HTTPS_PROXY`; the proxy injects each into requests to its own hosts (the PAT for
GitHub hosts, the Anthropic secret for `api.anthropic.com`) and tunnels
everything else. The agent holds only dummies, so it can't read the real
secrets: a different UID with no sudo can't read the proxy's
`/proc/<pid>/environ`, the credential `actions/checkout` persists in
`.git/config` is stripped before the workspace is handed over, and the model
auth is never written to the agent's env or disk. The injection allowlist is
exact-match on the connection's real destination, so a request to a lookalike
host gets no token. (`claude` is Node and ignores the system trust store, so it
trusts the proxy CA via `NODE_EXTRA_CA_CERTS`.) The Codex harness
(`codex/action.yaml`) still passes both the PAT and the model auth directly to
the agent. The merge restriction and `tend check` remain the load-bearing
boundaries regardless of harness.

**Rate limiting.** Burst detection (10 PRs or issues per 20 minutes) and
spike detection (today's volume vs 6-day baseline, scaled per repo) abort
the run before Claude starts, catching runaway loops between workflows.
The check runs as a shell step, so a prompt-injection attack inside the
Claude session cannot skip it. Concrete limits live in
`shared/steps/rate-limit-preflight.sh`.

**Fixed prompts and marketplace skills.** The prompt and skill set come from
the composite action and the tend marketplace, not from the PR. An attacker
can influence what Claude *reads* (the diff, the issue body) but not the
*instructions* Claude follows or the *tools* it has access to.

**Environment-protected secrets.** Release secrets (registry tokens,
signing keys) live in GitHub Environments whose `deployment_branch_policy`
lists only admin-gated refs: the default branch (merge restriction) and
all tags (a sibling tag-target ruleset that gates `creation` and `update`
with admin-only bypass; `update` is what force-push of an existing tag
fires, so it must be blocked alongside `creation`). The bot has write,
which is below every role that can bypass, so it cannot push to the
default branch and cannot push any tag, and therefore cannot reach any
environment pinned to those refs. The chain holds for workflows whose
only path to invocation is updating one of those refs: trigger on
`push: tags:` (release) or
`push: branches: [main]` (continuous deploy). Other triggers
(`workflow_dispatch`, `release: published`, `deployment`, `schedule`,
chained dispatches) can be initiated by a write-scoped bot against an
allowed ref, so the env policy alone does not gate them; workflows
keeping those triggers need trigger-specific containment (typically
required reviewers on the Environment) before release or deploy secrets
are migrated there. The chain inherits the merge restriction's
assumption that the bot holds no role that can bypass; an admin session
voids both the same way. `tend check` verifies both halves: the bot's
role, and that every bypass actor on the merge ruleset outranks write.

OIDC-to-cloud deploys have no GitHub-stored secret to gate; there, the
Environment plus the cloud provider's trust policy is the only control.

Configuration recipe:
`plugins/install-tend/skills/install-tend/references/security-model.md`.

**GitHub's log masking.** Secrets stored in GitHub are automatically redacted
from workflow logs. This is exact-match only — if a token appears
base64-encoded or embedded in JSON, the redaction misses it.

## Remaining risks

**Claude executes attacker-controlled code.** This is the biggest open gap.
When Claude runs tests or build commands on a fork PR, it executes code the
attacker wrote. A `Makefile`, `package.json` postinstall hook, or
`conftest.py` can do anything the runner can — including reading environment
variables and sending them over the network. Config pinning prevents
*Claude Code's own* startup hooks from being hijacked, but it can't prevent
Claude from voluntarily running `make test` on a repo where `make test` has
been weaponized. The Codex harness makes this explicit: its composite
action runs with `sandbox: danger-full-access`, deliberately not relying
on codex's inner bwrap jail. The ephemeral single-use runner VM is the
isolation boundary; the inner sandbox is redundant there and unavailable
on the standard runner image anyway. The boundaries that are load-bearing
(merge restriction, scope-limited credentials) sit outside the harness's
local-exec sandbox regardless.

**Repo write access implies secret access.** The bot PAT and the harness
token are repo-level secrets, readable by any workflow the repo runs; unlike
the release secrets above, they are not environment-gated. Like any GitHub
Actions secret, a write-scoped actor (a leaked PAT, or a hijacked session that
can push a branch) can commit a workflow that prints them and read them from
its own run. Branch protection bounds what gets *merged*; it does not bound
what a write-scoped run can *read*, and the merge restriction does not apply
because nothing is merged. Forks cannot reach them: GitHub withholds secrets from
fork-PR workflows, and the secret-bearing events (`pull_request_target`, the
review events, `schedule`) run only the default branch's reviewed workflow
files. Pinning these secrets to the default-branch ref with a GitHub
Environment does not close the gap: the policy keys on `GITHUB_REF`, which
cannot tell a legitimate `tend-mention` review event (carrying
`refs/pull/N/merge`) from a same-repo-PR exfiltration on the same ref, so it
would break mention's review-comment paths without gaining protection; the full
analysis is in `TODO.md`.

**Token exfiltration via side channels.** Log masking only catches exact
string matches in stdout. An attacker who gets code execution can exfiltrate
tokens via DNS queries, HTTP requests to an external server, or encoding
tricks that bypass the log filter. On GitHub-hosted runners, there's no way
to restrict outbound network access. For the model auth specifically,
Claude Code's bubblewrap sandbox would remove it from the agent's Bash tool
entirely: a probe confirmed the sandbox's fresh `/proc` mount and `denyRead`
rules block reading the token from the environment, `/proc`, and credential
files. It is not deployed because the same bwrap path corrupts `!` in Bash
commands (anthropics/claude-code#64301). On both Claude harnesses this is now
moot — the credential proxy keeps the real model auth out of the agent's env
entirely, so there is nothing for bwrap to hide. The Codex harness still passes
its model auth (an OpenAI key) directly, but runs with `sandbox:
danger-full-access` by design (see "Claude executes attacker-controlled code"
above). See the `TODO.md` entry and #639.

**Long-lived PAT exposure.** A classic PAT is valid until revoked and grants
access to every repo the bot account can reach. A single successful
exfiltration gives the attacker persistent, broad write access. The merge
restriction limits what they can *do* with it, but they can still push
branches, create PRs, and post comments indefinitely. The credential isolation
above keeps both the PAT and the Claude token out of the agent on both Claude
harnesses; both remain directly exposed on the Codex harness.

**Prompt injection without code execution.** Even without hijacking the
tools, an attacker who controls what Claude reads can influence its behavior.
A carefully crafted PR description or issue body could get Claude to approve a
bad PR, post misleading comments, or dismiss legitimate review concerns. Fixed
prompts and skill instructions reduce this risk but can't eliminate it —
Claude ultimately reasons about attacker-controlled text.

Deferred hardening options (Haiku pre-screening, read-only fork PRs, network
isolation, the Bash sandbox, workflow-dispatch isolation, GitHub App in
place of PAT) live in `TODO.md`.
