# CI Automation Security Model

Tend gives an AI agent write access to a repository and runs it on
attacker-controlled input (PR diffs, issue bodies, comments, CI logs). The
agent uses authenticated GitHub and model connections to push commits, post
reviews, and create PRs. The security model keeps the PAT and long-lived model
credentials outside the agent process and requires a human to land code. The
agent is expected to use the GitHub API for any repository the bot account can
access, including repositories other than the one that started the run.

Each adopting repo should document its specific configuration (admin accounts,
token names, protected environments) in its own
`.claude/skills/running-tend/SKILL.md`, the adopter-owned overlay the rest of
the docs name. Not a `docs/agent-notes.md` of its own: PR instruction
pinning covers `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, and `.claude/` at
any depth under both harnesses (`shared/steps/restore-sensitive-config.sh`), so
notes parked outside those paths are read from the PR's own tree.

## Threats

Three things an attacker wants, roughly in order of severity:

1. **Merge malicious code to the default branch.** Game over — the attacker
   controls the repo. Everything else is damage limitation compared to this.

2. **Exfiltrate tokens.** Code running inside the agent must cross the UID
   boundary or compromise a runner-owned proxy to steal the bot PAT or API
   credentials. Subscription consumers do receive an expiring access token,
   but never its rotating refresh token. A stolen PAT grants persistent GitHub
   access; stolen model auth grants billed model access.

3. **Hijack a single session.** An attacker who controls what the agent does
   in one run can push malicious branches, post misleading reviews, or create
   spam PRs.

The attack surface varies by workflow. `tend-review` is the most exposed —
the attacker controls the entire PR diff, which Claude reads and reasons
about. `tend-weekly` is the least exposed — triggered on a cron with no
user-controlled input.

The merge restriction, the environment gate on the operational secrets, and
fixed prompts apply to every workflow; the table lists what is specific to
each.

| Workflow | Injection surface | Attacker control | Specific mitigations |
|----------|-------------------|-------------------|-------------|
| **review** | PR diff content, review body on bot PRs | Full (any PR) / Medium (reviewers) | Base-branch config restoration |
| **triage** | Issue body | Partial (structured skill) | Structured skill |
| **mention** | Comment body on any issue/PR | Full | Engagement verification; review events re-entered via a secretless relay |
| **ci-fix** | Unsuccessful CI logs | Minimal (must disrupt CI on default branch) | Automatic trigger |
| **weekly** | None | None | Scheduled trigger |

## What we do

Three load-bearing boundaries:

1. **The bot cannot land code.** A merge restriction keeps every protected
   branch behind a human; where releases rely on tags, an all-tags ruleset
   does the same for tags.
2. **A run the bot can cause reads no secrets.** Every stored secret sits
   behind a gate the bot cannot pass, or is explicitly allowlisted in the
   tend config as accepted repo-level exposure.
3. **Future published releases cannot be rewritten.** GitHub immutable
   releases lock the release record, its assets, and the associated tag from
   the point the repository setting is enabled.

`tend check` fails until the first two hold and the third is enabled, so a
passing check *is* the claim for future releases. GitHub does not apply the
setting retroactively.

**Merge restriction.** A GitHub ruleset (or branch protection) prevents the
bot from merging to protected branches (the default branch plus any in
`protected_branches`) regardless of review status. The composite action's
preflight verifies this as the bot itself: `current_user_can_bypass` on
each applying ruleset is GitHub's own evaluation of the bot's standing —
teams, custom roles, and org-level rulesets included — and the run aborts
if the bot can bypass every restrict-updates ruleset, or if the branch is
unprotected entirely.

**Environment-gated secrets.** A job that names a GitHub Environment runs
only if the run's `GITHUB_REF` matches the environment's deployment branch
policy; otherwise the job is refused before its first step, and the
environment's secrets are released only to jobs that name it. Pinning the
policy to refs only admins can move therefore decides secret access by
ref. The bot has write, so it can move neither the default branch (merge
restriction) nor any tag (tag ruleset), and managing environments — the
policy and the secrets inside — requires admin, which the bot also lacks.

The claim in sentence 2 is the conjunction of three checks, each keyed on
where a credential can live:

- *Every credential-holding environment is gated*
  (`credential-environments`): a required reviewer who is not the bot, or
  a deployment policy naming only verified refs — branches the same run
  confirmed the bot cannot write, or tags under an admin-only all-tags
  ruleset — with no workflow reaching it on a trigger the bot steers. This
  covers release tokens exactly as it covers tend's own secrets, keyed on
  holding a credential rather than on any environment name. A credential
  is a stored secret, or the OIDC token a job minting `id-token: write`
  in the environment's name can spend: trusted publishing (PyPI, npm, a
  cloud role) stores nothing, so a sweep reading stored secrets alone
  walks past exactly the repos that publish.
- *No repo-level secret outside the allowlist* (`repo-secret-allowlist`):
  a repo-level secret is readable by any workflow the repo runs, so each
  one must be a deliberate `secrets.allowed` entry — and the operational
  names are refused there at config load, so no one config line can
  reopen the gate. Org-level secrets are swept into the same check
  best-effort: they cannot be environment-gated at all, and listing them
  needs `admin:org`, the one place the claim rests on the token the
  maintainer ran `tend check` with. Only the org secrets this repo can
  actually read count — one scoped away from it (`selected` without the
  repo, `private` against a public repo, any of them against a private
  repo in a GitHub Free org) reaches no workflow here, and naming it
  would leave a failure no repo-side change can clear. A secret whose
  reach cannot be determined is reported rather than dropped.
- *The operational secrets actually live in the gated environment*
  (`environment`, `secrets`): the `tend` policy admits exactly the
  verified branches, and the bot PAT and harness auth are present there
  rather than anywhere flatter.

What a pushed workflow holds, then, is only what GitHub gives every run:
its ephemeral `GITHUB_TOKEN`, at whatever permissions the file declares —
bounded by the same rulesets (it cannot merge or tag), unable to read any
secret value back through the API, and expiring with the job.

*Operational secrets* — the bot PAT, harness auth, the Codex subscription
refresher's credential, and optional auto-memory Gist ID — live in the `tend`
environment, whose policy names the default branch and any
`protected_branches`. Every generated job that reads a secret carries
`environment: {name: tend, deployment: false}`; jobs that hold none
(mention's relay, below) must not, since naming it would cost them the refs
the policy excludes. `deployment: false` keeps GitHub from filing a
deployment record for a job that deploys nothing — under
`pull_request_target` those land on the pull request itself, one line per
push — and leaves the policy check untouched. This closes
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
| `pull_request_target` | PR base branch | passes for admitted bases | ✓ |
| `push` to a feature branch | that branch | refused | ✓ |
| same-repo `pull_request` | `refs/pull/N/merge` | refused | |
| `pull_request_review`, `pull_request_review_comment` | `refs/pull/N/merge` | refused | ✓ |

The `pull_request_target` refusal on other bases is itself load-bearing:
that event runs the *base* ref's workflow file, so a PR targeting a
bot-pushable branch would execute whatever tend-review.yaml that branch
carries, with the secrets. The cost is that a stacked PR — one based on
another PR's branch — gets no review until it retargets an admitted
branch; the refused run fails visibly rather than skipping. (The probe
observed the default-base case; the base-ref value is GitHub's documented
`GITHUB_REF` for the event, and the refusal is the mechanism the `push`
row measures.)

Only one workflow legitimately needs a refused ref: tend-mention answers
review submissions and inline review comments. The merge ref can
never be admitted, because a same-repo `pull_request` run executes the PR
head's own workflow files on that same ref — admitting it would hand a
pushed workflow the secrets back. So tend-mention re-enters those events
instead: a secretless `relay` job (only the workflow-scoped `GITHUB_TOKEN`,
`contents: write`, which is what the dispatch POST requires — `read` is
refused 403, probed) receives the review event and re-posts it as a
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
files, but that run's token is read-only whatever the file asks for: probed
with `contents: write` declared, its `POST /repos/…/dispatches` still
returns 403, where the same request from a same-repo run with the same
permission succeeds. So a fork PR's reviews reach the bot through the
notifications poll, minutes later rather than seconds. The daily live scan is
the slower backstop for a missed notification. That is the cost of the
property the relay depends on: a fork run that could start a secret-bearing
run in the base repo would be a fork run with write access to it.

*Release secrets* (registry tokens, signing keys) use the same mechanism in
adopter-owned environments whose policies list the default branch and/or
all tags (a tag-target ruleset gates `creation` and `update` with
admin-only bypass; `update` is what force-push of an existing tag fires, so
it must be blocked alongside `creation`). Rulesets are the only mechanism —
GitHub sunset tag protection rules in 2024 — and the `creation` rule also
refuses `POST /repos/{repo}/releases` when the named tag does not exist
yet, so the Releases API is not a way around it. The
`credential-environments` sweep above verifies every such environment.

Immutable releases close the separate write path: once a release is published,
GitHub locks its assets and associated tag. This is a repository setting, not a
ruleset inference; `tend check` verifies it directly and `--fix` enables it.
The setting is prospective, so enable it before the repository's next release.
It does not make `release: published` safe for secrets: a write actor
can still publish a new release against an existing unpublished tag.

The gate bounds what a run can *read*; it does not by itself bound *when*
a reviewed workflow fires. A workflow reachable only by updating a gated
ref (`push: tags:` for release, `push: branches: [main]` for continuous
deploy) is fully chained: causing the run at all takes an admin action, and
the code the run executes is fixed by the ref, so the worst a write-scoped
bot achieves is re-publishing what an admin already published. `schedule`,
`workflow_run`, `deployment` and an input-free `workflow_dispatch` sit in
that class too.

Three triggers do not, because the bot supplies the run's payload as well
as firing it, and it fires them at a ref the policy already admits:
`release: published` (creating a release against an existing tag takes no
tag operation, and the release's body and assets are the bot's own),
`repository_dispatch` (`client_payload` wholesale), and a
`workflow_dispatch` carrying inputs. A ref policy cannot gate these; only a
required reviewer can, since it holds every trigger regardless of ref. The
sweep therefore refuses a ref-gated environment that a workflow reaches on
one of the three. A workflow that must run on one puts its secrets in a
second environment behind a required reviewer instead of a branch policy,
so each run waits for a human; the sweep verifies any such environment,
keyed on the credential rather than the name, with the bot excluded from
the reviewer list since a bot that can approve its own run makes the wait
a formality.

An OIDC publish or deploy (PyPI or npm trusted publishing, a cloud role)
stores no secret, so the environment is the whole gate on GitHub's side —
the token's `sub` names it, and a relying party can require that claim. A
job holding `id-token: write` outside any environment has no gate at all:
the token carries no environment claim, and the bot can mint it from a
branch it pushes, which any trust policy pinning the repository but not the
ref accepts. The sweep covers both cases: an environment a job mints OIDC
in holds a credential even with nothing stored in it, and a job minting one
outside any environment is reported on its own. Tend's own generated
workflows request no `id-token`.

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
immutability is the boundary this relies on for new releases: GitHub's
immutable-releases setting locks each release, its assets, and its tag when it
is published. The tag ruleset also restricts updates. Tend's releases from
before the setting was enabled have no uploaded assets and their tag code is
protected by a no-bypass tag ruleset, but their GitHub release records are not
retroactively immutable. The separate all-tags ruleset prevents the bot from
creating or repointing any release tag, so a leaked bot token or hijacked
session cannot change the action code every adopter already runs. Adopters
extend trust to `max-sixty/tend`'s release-tag integrity the same way they
trust any third-party action's publisher; pinning to `X.Y.Z` (or a commit
SHA) bounds that trust to a reviewed, immutable point.

**Config pinning.** Both harness actions restore RCE-relevant config from
the PR base branch before the agent starts: `.claude/`, `.mcp.json`, `.claude.json`,
`.gitmodules`, `.ripgreprc`, `.husky` at the root, plus — as a prompt-injection
defense — every `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, and `.claude/` at
any depth, since Claude Code loads the instruction file nearest the file the
agent opens and the skills under any directory's `.claude/`. A malicious PR's
`SessionStart` hook, MCP server, or injected `CLAUDE.md` is reverted before
Claude reads it. The restoration is `git restore --source=<base>` in shell:
base-branch versions are written back, fork-added paths removed, and a
fork-planted symlink replaced rather than written through. The root path list
and ordering mirror claude-code-action's `restore-config.ts`. The PR's own
versions stay readable at `git show HEAD:<path>` for a review that wants to see
what it changed; nothing copies them into the worktree, since a copy made by
the runner user would follow a fork-planted symlink into files the agent must
never see, such as the checkout credential in `.git/config`.

**Setup runs on reviewed code.** Adopter `setup:` steps execute as the runner
user, which holds sudo and, until the sandbox setup strips it, the checkout
PAT in `.git/config`. So every generated workflow checks out reviewed code
before running them — the default branch, or in `tend-review` the PR's base
branch — and lands the PR's tree only afterwards. A contributor's build
backend, added dependencies, and local `uses: ./` actions therefore execute
under the agent, inside the sandbox, rather than ahead of it. `sandbox_setup:`
is the lever for project setup that must see the PR's own manifests. Both
harnesses run it as the non-sudo sandbox user.

**Credential isolation.** Both harness actions run the agent as a separate
non-sudo `tend-sandbox` user, sharing the GitHub proxy machinery under the
top-level `proxy/`. The bot PAT lives only in a local mitmproxy that the agent
reaches over `HTTPS_PROXY`; the proxy injects it only for exact GitHub hosts
and tunnels everything else. This authenticates API and git operations for any
repository the bot account can access; credential isolation protects the PAT
itself rather than restricting those operations to the triggering repository.
Claude's Anthropic credential (OAuth token or API key) uses the same proxy and
is injected only for `api.anthropic.com`. Under API auth, Codex's OpenAI key is
instead read from stdin by OpenAI's hardened Responses API proxy. It forwards
only `POST /v1/responses` upstream and answers `GET /shutdown` on loopback so
Tend can stop it during teardown. The agent holds only a dummy PAT and the
local inference endpoint. Under subscription auth, it additionally receives an
expiring access-only `auth.json`, but not the rotating refresh token. A
different UID with no sudo cannot read either proxy's
`/proc/<pid>/environ`, the credential `actions/checkout` persists in
`.git/config` is stripped before the workspace is handed over, and the PAT and
API credentials are never written to the agent's env or disk. The injection
allowlist is exact-match on the connection's real destination, so a request to
a lookalike host gets no token. The GitHub proxy is launched by a pinned `uv`
that Tend installs into its own directory, off `$PATH`, so the process holding the PAT
starts from a known binary rather than whatever an adopter's
`setup:` happened to leave on the runner. (`claude` is Node and ignores the
system trust store, so it trusts the proxy CA via `NODE_EXTRA_CA_CERTS`.) Shared
system and hosted-toolcache PATH entries remain available to the sandbox. Tend
appends a pinned `uv` fallback after those paths before `sandbox_setup:` runs. A
runner-home PATH entry may select an independently seeded directory already
owned by the sandbox user; runner-home files themselves stay off the sandbox
PATH except for checkout paths. Tend does not infer which files under the
runner home are runtimes rather than secrets; later home-scoped changes must be
made as the sandbox user with `sandbox_setup:`. A generic failure shim keeps a
dropped home-selected command from silently falling through to a different
same-named system tool.

**Session-log upload.** The token-usage step uploads the agent's session JSONL
as an artifact, so the runner has to read a tree the sandbox user owns. It
copies as root and chowns the result rather than making the source readable,
since a `chmod -R` aimed through a symlink the agent planted would grant read on
whatever tree it named.

The agent chooses the input to every check that follows. The copy waits for the
harness to reap every sandbox process, so nothing is left alive to change
what was checked. The session directory and the dot-directory above it are
refused if either is a symlink. The copied modes are reset to the runner's,
since deleting an entry needs write on its parent and those modes came from the
agent. Every entry that is not a regular file or a directory is deleted before
`upload-artifact` reads it: a symlink it would otherwise resolve as the runner,
a FIFO it would block on until the job times out.

The weekly subscription refresh job checks out no adopter code and gives Codex
only Tend's fixed refresh prompt. Codex receives the full refresh bundle there;
the environment-write PAT appears only in the separate publish step after
Codex exits.

**Rate limiting.** Burst detection (10 PRs or issues per 20 minutes) and
spike detection (today's volume vs 6-day baseline, scaled per repo) abort
the run before the agent starts, catching runaway loops between workflows.
The check runs as its own step in the composite action, so a
prompt-injection attack inside the agent session cannot skip it. Concrete limits live in
`shared/steps/rate_limit_preflight.py`.

The spike limit is resumable by a maintainer, the burst limit is not. On a
spike trip the run files or reopens a `tend-rate-limit` issue listing the
runs it refused; closing that issue doubles the ceiling for the rest of the
UTC day, and each further close doubles it again, so the limit re-arms
after use rather than switching off. Approval is a check rather than an
instruction: the preflight counts only closes whose actor is not the bot,
and since GitHub admits only the author or a triage/write collaborator to
close an issue — and the bot is the author — that leaves exactly the
maintainers. The bot cannot approve itself even if a prompt injection tells
it to, and there is no allowlist to maintain.

Refused runs do not retry on their own; the issue's table carries their
links. Automating that is deferred (see `TODO.md`).

**Fixed prompts and marketplace skills.** The prompt and skill set come from
the composite action and the tend marketplace, not from the PR. An attacker
can influence what the agent *reads* (the diff, the issue body) but not the
*instructions* it follows or the *tools* it has access to.

**GitHub's log masking.** Secrets stored in GitHub are automatically redacted
from workflow logs. This is exact-match only — if a token appears
base64-encoded or embedded in JSON, the redaction misses it.

## Remaining risks

**The agent executes attacker-controlled code.** This is the biggest open gap.
When an agent runs tests or build commands on a fork PR, it executes code the
attacker wrote. A `Makefile`, `package.json` postinstall hook, or
`conftest.py` can do anything the sandbox user can and send data over the
network. It cannot read the PAT or API credentials; subscription mode's
expiring access token is the deliberate exception described below. Config
pinning prevents
*Claude Code's own* startup hooks from being hijacked, but it can't prevent
an agent from voluntarily running `make test` on a repo where `make test` has
been weaponized. The experimental Codex harness currently defaults to
`sandbox: danger-full-access`. On the standard GitHub-hosted Ubuntu 24.04
runner, Codex's restricted Linux sandbox cannot initialize bubblewrap's
loopback network, so those modes do not currently run in Tend's default
environment. The ephemeral runner VM contains local execution, and the
separate UID plus credential proxy keeps the PAT and API credentials outside
the agent process.

**Write access still starts workflows.** With the operational secrets
environment-gated, a write-scoped actor can no longer read them out of a
workflow it pushes; what it keeps is invocation. It can post the comments
and reviews that wake the bot, and it can forge the `repository_dispatch`
that tend-mention's relay uses — both start only the default branch's
reviewed workflow files, with the engagement checks applied to the record
GitHub holds rather than to the payload.

**Data exfiltration via side channels.** An attacker who gets code execution
can exfiltrate repository contents and agent-visible context via DNS queries,
HTTP requests to an external server, or workflow logs; on GitHub-hosted
runners there's no way to restrict outbound network access. Credential
isolation keeps the PAT and API credentials out of what a hijacked session can
send. A Codex subscription session can send its expiring access token, but it
never receives the rotating refresh token or the PAT that rewrites environment
secrets.

**Credential theft.** Isolation minimizes the chance that a hijacked session
can steal the long-lived tokens, but it does not protect against compromise of
the runner-owned proxy or the runner itself. A stolen classic PAT remains valid
until revoked and grants access to every repository both its scope and the bot
account can reach. A stolen subscription access token remains valid until it
expires. The merge restriction, environment gate, and immutable releases limit
what the stolen GitHub credential can do.

**Prompt injection without code execution.** Even without hijacking the
tools, an attacker who controls what Claude reads can influence its behavior.
A carefully crafted PR description or issue body could get Claude to approve a
bad PR, post misleading comments, or dismiss legitimate review concerns. Fixed
prompts and skill instructions reduce this risk but can't eliminate it —
Claude ultimately reasons about attacker-controlled text.

**Persistent auto memory.** The experimental `memory_gist: true` setting lets
Claude carry model-authored notes into unrelated later runs. Those notes are
context, not policy, and may preserve stale facts or the effect of an earlier
prompt injection. The adapter accepts only a bot-owned secret Gist bound to the
exact repository, signs its per-run baseline, rejects symlinks and nested paths,
and skips the entire save when it observes a conflict. A secret Gist is readable
to anyone who learns its URL, so the Gist ID stays out of committed public files
and the experiment is refused for private repositories. It is not hidden from
the session: the agent's proxied bot access can list the account's Gists.

Deferred hardening options (Haiku pre-screening, read-only fork PRs, and
network isolation) live in `TODO.md`.
