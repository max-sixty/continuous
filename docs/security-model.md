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

The merge restriction, the environment gate on the operational secrets, and
fixed prompts apply to every workflow; the table lists what is specific to
each.

| Workflow | Injection surface | Attacker control | Specific mitigations |
|----------|-------------------|-------------------|-------------|
| **review** | PR diff content, review body on bot PRs | Full (any PR) / Medium (reviewers) | CLAUDE.md pinning (fork PRs) |
| **triage** | Issue body | Partial (structured skill) | Structured skill |
| **mention** | Comment body on any issue/PR | Full | Engagement verification; review events re-entered via a secretless relay |
| **ci-fix** | Failed CI logs | Minimal (must break CI on default branch) | Automatic trigger |
| **weekly** | None | None | Scheduled trigger |

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

**Environment-gated secrets** covers what a run can read. A job that names
a GitHub Environment runs only if the run's `GITHUB_REF` matches the
environment's deployment branch policy; otherwise the job is refused before
its first step, and the environment's secrets are released only to jobs
that name it. Pinning the policy to refs only admins can move therefore
decides secret access by ref. The bot has write, so it can move neither the
default branch (merge restriction) nor any tag (tag ruleset), and managing
environments — the policy and the secrets inside — requires admin, which
the bot also lacks.

Tend applies this to both secret classes.

*Operational secrets* — the bot PAT and the harness auth — live in the
`tend` environment, whose policy names the default branch and any
`protected_branches`. Every generated job that reads a secret carries
`environment: tend`; jobs that hold none (mention's relay, below) must not,
since naming it would cost them the refs the policy excludes. This closes
the classic no-merge exfiltration: a write-scoped actor (a leaked PAT, or a
hijacked session that can push a branch) commits a workflow that prints the
secrets and reads them from its own run. Branch protection never touched
that path — it bounds what gets *merged*, not what a run can *read* — but
the environment does: the pushed workflow's run carries the branch's own
ref, and the job naming the environment fails with zero steps executed
(observed on a live probe).

Where each trigger runs. A ✓ row was observed on a live probe; the rest
carry the ref of the family they belong to and were not probed
individually. GitHub's "runs in the context of the default branch"
sentence for review events refers to which workflow *file* runs, not to
`GITHUB_REF` — which is why the review rows had to be measured rather than
read off the docs:

| Trigger | `GITHUB_REF` | `tend` gate | |
|---|---|---|---|
| `issue_comment`, `repository_dispatch` | default branch | passes | ✓ |
| `issues`, `schedule`, `workflow_run` | default branch | passes | |
| `pull_request_target` | default branch | passes | ✓ |
| `push` to a feature branch | that branch | refused | ✓ |
| same-repo `pull_request` | `refs/pull/N/merge` | refused | |
| `pull_request_review`, `pull_request_review_comment` | `refs/pull/N/merge` | refused | ✓ |

Only one workflow legitimately needs a refused ref: tend-mention answers
review submissions and inline review comments. The merge ref can
never be admitted, because a same-repo `pull_request` run executes the PR
head's own workflow files on that same ref — admitting it would hand a
pushed workflow the secrets back. So tend-mention re-enters those events
instead: a secretless `relay` job (only the workflow-scoped `GITHUB_TOKEN`,
`contents: read`) receives the review event and re-posts it as a
`repository_dispatch` carrying identifiers only (`{kind, pr, id}`). The
dispatch run carries the default branch, passes the gate, and its verify
job re-reads the review or comment from the API before applying the
engagement checks. Any write-scoped actor can forge such a dispatch, which
is why the payload carries no judgement: a forged dispatch runs the same
reviewed workflow file, faces the same engagement checks against the record
GitHub holds, and can point the bot at nothing the actor couldn't reach by
posting a comment.

The relay stops at the repository boundary. A review event on a *fork* PR
does start a run, on the merge ref and from the PR head's own workflow
files, but that run's token is read-only: its `POST /repos/…/dispatches`
returns 403 (probed). So a fork PR's reviews reach the bot only through the
notifications poll, minutes later rather than seconds. That is the cost of
the property the relay depends on: a fork run that could start a
secret-bearing run in the base repo would be a fork run with write access
to it.

*Release secrets* (registry tokens, signing keys) use the same mechanism in
adopter-owned environments whose policies list the default branch and/or
all tags (a tag-target ruleset gates `creation` and `update` with
admin-only bypass; `update` is what force-push of an existing tag fires, so
it must be blocked alongside `creation`). The chain holds for workflows
whose only path to invocation is updating one of those refs: trigger on
`push: tags:` (release) or `push: branches: [main]` (continuous deploy).
Other triggers (`workflow_dispatch`, `release: published`, `deployment`,
`schedule`, chained dispatches) can be initiated by a write-scoped bot
against an allowed ref, so the env policy alone does not gate *when* they
fire; workflows keeping those triggers need trigger-specific containment
before release or deploy secrets are migrated there. Tend's convention for
one is a second environment, `tend-manual`, holding the same secrets behind
a required reviewer instead of a branch policy, so each run waits for a
human. `tend check` verifies it wherever a repo declares one — reviewers
present, and the bot not among them, since a bot that can approve its own
run makes the wait a formality. OIDC-to-cloud deploys have no GitHub-stored
secret to gate; there, the Environment plus the cloud provider's trust
policy is the only control.

*Migration.* Environment secrets overlay repo-level ones, and a job naming
an environment that does not yet exist auto-creates it with no policy and
runs normally (observed on a live probe). A repo that has not completed the
migration therefore keeps working on its repo-level secrets, with exactly
its old exposure — the gate protects nothing until the policy is set, the
secrets are moved, and the repo-level copies are deleted. `tend check`
fails on each missing piece until then: it verifies the environment exists,
its policy is a named list matching exactly the branches whose protection
that same run confirmed, the operational secrets are present in it, and no
repo-level copy remains. Confirmed, not configured: a branch named in
`protected_branches` that does not exist yet cannot be admitted, or the
policy would name a ref the bot can create — and the merge restriction
gates `update`, not `creation`. `tend check --fix` creates the environment and
reconciles its policy; moving the secrets stays manual, since their values
cannot be read back.

The policy must be that named list rather than GitHub's "protected
branches" mode, which keys on whether *a* rule covers the branch and not on
who may push it. Probed: with that mode selected, a branch protected only
by `required_linear_history` — which blocks no push — took a plain push and
then read an environment secret, while an unprotected branch was refused
with zero steps.

Both environment chains inherit the merge restriction's assumption that the
bot holds no role that can bypass; an admin session voids all of it the
same way. Configuration recipe:
`plugins/install-tend/skills/install-tend/references/security-model.md`.

Everything else in this section is defense in depth: useful, but not
load-bearing.

**Action distribution integrity.** Generated workflows pin the composite
action to the generator's own release version
(`max-sixty/tend/<harness>@X.Y.Z`), never a floating ref. Release-tag
immutability is the boundary this relies on: a `tag` ruleset on
`max-sixty/tend` restricts `update` and `deletion` on `refs/tags/[0-9]*`
and lists no bypass actors at all, so a published release tag cannot be
moved or deleted by anyone. Unlike the merge restriction, an admin session
does not void it. `creation` stays open so a release can push a new
`X.Y.Z`. A leaked bot token or hijacked session therefore cannot
retroactively change the code every adopter already runs; the worst it can
do is add a release tag, which adopters only pick up on their next nightly
regen, as a reviewable workflow-file diff in their own repo. Adopters
extend trust to `max-sixty/tend`'s release-tag integrity the same way they
trust any third-party action's publisher; pinning to `X.Y.Z` (or a commit
SHA) bounds that trust to a reviewed, immutable point.

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
the agent. The merge restriction and the environment gate remain the
load-bearing boundaries regardless of harness.

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

**Write access still starts workflows.** With the operational secrets
environment-gated, a write-scoped actor can no longer read them out of a
workflow it pushes; what it keeps is invocation. It can post the comments
and reviews that wake the bot, and it can forge the `repository_dispatch`
that tend-mention's relay uses — both start only the default branch's
reviewed workflow files, with the engagement checks applied to the record
GitHub holds rather than to the payload. The secrets are also still in
memory during every legitimate run, so an attacker who gets code execution
inside one retains everything the side-channel entry below describes.

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
