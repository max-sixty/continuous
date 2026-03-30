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

**Merge restriction** is the primary security boundary. A GitHub ruleset (or
branch protection) prevents the bot from merging to protected branches
(default branch plus any in `protected_branches`) regardless of review status. The composite action refuses to start if the
default branch isn't protected. Everything below is defense in depth — useful,
but not load-bearing.

**Config pinning.** `claude-code-action` restores `.claude/`, `.mcp.json`,
`.claude.json`, `.gitmodules`, and `.ripgreprc` from the base branch on all
PRs. These paths give the CLI code execution at startup — hooks run shell
commands, `.mcp.json` spawns server processes, `.claude.json` can set
environment variables, `.gitmodules` can point submodules at attacker repos,
`.ripgreprc` can inject commands via ripgrep's `--pre` flag. A malicious PR
that adds a `SessionStart` hook or an MCP server gets those changes silently
reverted before Claude starts. The composite action separately pins
`CLAUDE.md` to the base branch on fork PRs — `claude-code-action` doesn't
cover this because CLAUDE.md is a prompt-injection vector, not an RCE
vector, and it's reasonable for same-repo PRs to modify their own
instructions.

**Rate limiting.** Burst detection (10 PRs and 10 issues per 20 minutes,
checked independently) and spike detection (today's volume vs 6-day baseline)
abort the run before Claude starts. This catches runaway loops — triage
creates a fix PR, CI fails, ci-fix creates another PR, repeat. Because the
check runs as a shell step before the Claude session, prompt injection can't
skip it.

| Check | Limit | Layer |
|-------|-------|-------|
| PRs created in last 20 min | 10 | Burst |
| Issues created in last 20 min | 10 | Burst |
| Items created today | 10 + 2× daily avg (past 6 days) | Spike |

The spike formula adapts to each repo's normal activity level: a repo that
averages 0 posts/day trips at 11, while one averaging 15/day trips at 41.
These are hardcoded in `action.yaml`. Because the check runs outside Claude's
session, a prompt injection attack cannot instruct the bot to skip it.

**Fixed prompts and marketplace skills.** The prompt and skill set come from
the composite action and the tend marketplace, not from the PR. An attacker
can influence what Claude *reads* (the diff, the issue body) but not the
*instructions* Claude follows or the *tools* it has access to.

**Environment-protected secrets.** Release secrets (registry tokens, signing
keys) should be in a GitHub Environment with deployment approval. Even if the
bot token leaks, the attacker can't exfiltrate environment-protected
secrets — those require a separate approval step. This matters because the
most dangerous escalation from a leaked bot token is pushing a branch with a
modified workflow that references repo-level secrets, then opening a PR — the
modified workflow runs from the PR branch and all repo-level secrets are
exposed.

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
been weaponized.

**Token exfiltration via side channels.** Log masking only catches exact
string matches in stdout. An attacker who gets code execution can exfiltrate
tokens via DNS queries, HTTP requests to an external server, or encoding
tricks that bypass the log filter. On GitHub-hosted runners, there's no way
to restrict outbound network access.

**Long-lived PAT exposure.** A classic PAT is valid until revoked and grants
access to every repo the bot account can reach. A single successful
exfiltration gives the attacker persistent, broad write access. The merge
restriction limits what they can *do* with it, but they can still push
branches, create PRs, and post comments indefinitely.

**Prompt injection without code execution.** Even without hijacking the
tools, an attacker who controls what Claude reads can influence its behavior.
A carefully crafted PR description or issue body could get Claude to approve a
bad PR, post misleading comments, or dismiss legitimate review concerns. Fixed
prompts and skill instructions reduce this risk but can't eliminate it —
Claude ultimately reasons about attacker-controlled text.

## What we could do but don't

**GitHub App instead of PAT.** App installation tokens expire in ~1 hour
and are scoped to specific repos. This is the single highest-impact
improvement for token leak risk. Not yet implemented because it requires
either per-adopter App registration (friction) or tend-hosted infrastructure
(Model A in DESIGN.md's Auth section).

**Haiku pre-screening of diffs.** Before the main Claude session starts, a
cheap fast-model pass could scan the diff for suspicious patterns:
modifications to build scripts, `curl | sh`, base64-encoded strings,
environment variable reads targeting known secret names. Cost is ~$0.001 per
PR. Rejected as a security *boundary* (trivial to evade) but potentially
useful as a tripwire against unsophisticated attacks. Not yet implemented.

**Read-only mode for fork PRs.** Restrict `allowed_tools` to `Glob`, `Grep`,
`Read`, and comment-posting MCP tools — no `Bash`, `Edit`, or `Write`. Claude
can review the diff and post comments but can't execute code or push commits.
This would close the "attacker-controlled code execution" gap entirely for
fork PRs. The tradeoff: the bot can't suggest fixes on fork PRs, only
review them.

**Network isolation.** Self-hosted runners with outbound traffic restricted
to GitHub API and Anthropic API endpoints would prevent token exfiltration via
HTTP/DNS. Not viable on GitHub-hosted runners and adds significant
infrastructure overhead for self-hosted setups.

**Subprocess environment scrubbing.** `claude-code-action` supports
`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`, which strips sensitive environment
variables before spawning subprocesses. Currently only activated when
`allowed_non_write_users` is set. Could be enabled for all fork PRs to make
naive `echo $GH_TOKEN` attacks fail — though a subprocess can read the
parent's unscrubbed environment via `/proc/$PPID/environ` (same-user, no
privilege barrier on GitHub-hosted runners).

**Workflow dispatch isolation.** Split each workflow into an analysis job
(runs with `GITHUB_TOKEN`, reads the diff, produces a plan) and a push job
(separate workflow triggered by `workflow_run`, uses the bot token). The bot
token never enters a job that touches attacker-controlled code. Significant
complexity increase — every workflow becomes two workflows with artifact
passing between them.

---

## Reference

### What each workflow needs to do

| Capability | Triage | Mention | Review | CI Fix | Nightly | Renovate |
|------------|:---:|:---:|:---:|:---:|:---:|:---:|
| Read issues/PRs | Yes | Yes | Yes | Yes | Yes | — |
| Comment on issues | Yes | Yes | Yes | — | Yes | — |
| Create branches | Yes | Yes | Yes | Yes | Yes | Yes |
| Push commits | Yes | Yes | Yes | Yes | Yes | Yes |
| Create PRs | Yes | Yes | — | Yes | Yes | Yes |
| Post PR reviews | — | — | Yes | — | — | — |
| Resolve review threads | — | — | Yes | — | — | — |
| Monitor CI | Yes | Yes | Yes | Yes | Yes | Yes |
| **Pushes must trigger CI** | **Yes** | **Yes** | **Yes** | **Yes** | **Yes** | **Yes** |

The last row matters: `GITHUB_TOKEN` pushes don't trigger downstream workflows
(GitHub prevents infinite loops). Workflows that push code and need CI to run
**must** use a PAT or GitHub App installation token.

### Token assignment

Use a single bot token across all Claude workflows for consistent identity.
The merge restriction (ruleset) caps blast radius regardless of which token
is used.

Two tokens are needed:

| Token | Purpose |
|-------|---------|
| Bot token (PAT or App) | GitHub API and git operations. Consistent bot identity. |
| Claude OAuth token | Authenticates Claude Code to the Anthropic API. |

**Why one bot token.** The bot token is equally safe in any workflow because
the merge restriction caps the blast radius. Using a single token gives
consistent identity for reviews and comments and avoids the
`github-actions[bot]` branding.

**If a token leaks:**

| Token | Lifetime | If leaked, attacker can... | ...but cannot |
|-------|----------|---------------------------|---------------|
| Bot token (PAT) | Long-lived | Push to unprotected branches, create PRs, impersonate bot — **indefinitely** | Merge PRs (merge restriction), push to default branch, access release secrets (environment-protected) |
| Bot token (App) | ~1 hour | Same as PAT, but only until token expires | Same + token auto-expires |
| Claude OAuth | Long-lived | Run Claude sessions billed to the account | Access GitHub |

`GITHUB_TOKEN` is ephemeral (single job) and automatically scoped by each
workflow's `permissions:` block. Not a meaningful leak target.

**How tokens interact with `permissions:` and `actions/checkout`.** Two
independent authentication paths exist in every workflow:

1. **Git CLI** (`git push`): authenticates with the token from
   `actions/checkout`. When no explicit token is passed, this defaults to
   `GITHUB_TOKEN` scoped by the `permissions:` block. When an explicit token
   is passed, that token's scopes apply instead.
2. **GitHub API** (`gh pr create`, `gh api`): `claude-code-action` overwrites
   the `GITHUB_TOKEN` env var with its `github_token` input.

All workflows should pass the bot token to both paths.

### GitHub API: event types for PR comments

GitHub treats PRs as a superset of issues. Comments on a PR arrive via
different event types depending on where they're posted:

- **Conversation tab** → `issue_comment` event. Runs in base repo context —
  secrets available even for fork PRs. The PR is at
  `github.event.issue.pull_request`. The PR number is
  `github.event.issue.number`.
- **Files changed (inline)** → `pull_request_review_comment` event. Runs in
  fork context — no secret access for fork PRs (same restriction as
  `pull_request`). The PR is at `github.event.pull_request`. There is no
  `github.event.issue`.
- **Review submission** → `pull_request_review` event (type: `submitted`). Same
  fork restriction as `pull_request_review_comment`. The review is at
  `github.event.review`. The PR is at `github.event.pull_request`.

Individual inline comments from a review also fire as separate
`pull_request_review_comment` events.

GitHub provides `pull_request_target` as a secrets-safe equivalent of
`pull_request`, but no such variant exists for `pull_request_review_comment` or
`pull_request_review` ([community discussion][gh-55940]). This means
`tend-mention` cannot respond to inline review comments on fork PRs.
Conversation-tab comments (`issue_comment`) are unaffected.

[gh-55940]: https://github.com/orgs/community/discussions/55940

### Rules for modifying workflows

- **No role-based gating**: Don't check `author_association` (OWNER, MEMBER,
  etc.) to decide whether to run. The merge restriction is the security
  boundary. Use technical criteria: fork detection, loop prevention, trigger
  phrases.
- **Adding `allowed_non_write_users`** to a workflow with user-controlled
  prompts requires security review.
- **All Claude workflows** must include
  `--append-system-prompt "You are operating in a GitHub Actions CI environment. Use /tend-ci-runner:running-in-ci before starting work."`.
- **Token choice**: All Claude workflows use the bot token for consistent
  identity.
- **`permissions:` block**: Set `contents: read` for read-only workflows.
- **Sensitive secrets** must be in protected environments, never repo-level.
