---
name: install-tend
description: Sets up tend (Claude-powered CI) on a GitHub repo. Creates config, generates workflows, configures secrets and branch protection via API, creates the bot account, and provisions the bot's auth token. Use when setting up tend on a new repo or when asked to install/configure tend.
---

# Install Tend

Set up tend on the current repo. Ask the user for the bot name if not provided.

Follow each step in order. Skip steps that are already done — check each
prerequisite before acting. Derive `REPO` once at the start:

```bash
gh auth status
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
```

## Browser sessions

Step 6 requires a browser session for the user to sign up the bot account.
Step 8's default path (`gh auth login --web`) works in any browser —
Chrome is no longer required, though `mcp__claude-in-chrome__*` automation
can drive either step if available. Otherwise, give the user URLs and wait
for confirmation. Before acting as the bot in any browser, verify the
logged-in user by clicking the avatar menu and checking the username.

## 1. Create config

Create `.config/tend.toml` with at minimum `bot_name`. See README.md for all
available config sections (`[secrets]`, `[setup]`, `[workflows.*]`).

```toml
bot_name = "<bot-name>"
```

Check whether the repo already has a bot token secret under a non-default name:

```bash
gh secret list --repo "$REPO" --json name --jq '.[].name'
```

If a bot-token-like secret exists (e.g., `GH_BOT_TOKEN`, `ROBOT_PAT`),
suggest overriding the default name rather than creating a duplicate:

```toml
[secrets]
bot_token = "GH_BOT_TOKEN"
```

If the secret list shows non-bot repo-level secrets (e.g., `CODECOV_TOKEN`,
`SENTRY_DSN`), add them to `secrets.allowed` so `tend check` doesn't flag them.
Any secret not in the allowlist triggers a warning — release secrets (registry
tokens, signing keys) should be in a protected environment, not listed here:

```toml
[secrets]
allowed = ["CODECOV_TOKEN"]
```

Discover existing CI workflows so tend-ci-fix can watch them:

```bash
grep -l 'push:\|pull_request' .github/workflows/*.yml .github/workflows/*.yaml 2>/dev/null
```

For each match, extract the workflow `name:` field. These are the workflows
that run tests, linting, or builds — tend-ci-fix should watch them. Configure:

```toml
[workflows.ci-fix]
watched_workflows = ["ci", "lint"]  # names of workflows to watch
```

If no CI workflows exist, either skip ci-fix (`enabled = false`) or help the
user create one first.

Ask the user about other overrides (setup steps, workflow overrides).

### Customizing generated workflow YAML

The generator owns every `tend-*.yaml` file — direct edits are lost on the next
`uvx tend@latest init`. Instead, set `workflow_extra` (top-level) or
`jobs.<name>` (job-level) overrides in `.config/tend.toml`. Overrides follow
RFC 7396 (JSON Merge Patch): mappings deep-merge, scalars and lists replace.

Common example — skip review on PRs labeled `tend:dismissed` (so authors can
opt out of re-reviews after the initial pass). Because scalars replace under
Merge Patch, the override must duplicate the default draft check:

```toml
[workflows.review.jobs.review]
if = "github.event.pull_request.draft == false && !contains(github.event.pull_request.labels.*.name, 'tend:dismissed')"
```

See `docs/tend.example.toml` in the tend repo for more override examples
(extending permissions, timeouts, top-level env vars).

## 2. Generate workflows

```bash
uvx tend@latest init
```

Verify workflow files appear in `.github/workflows/tend-*.yaml`. Run
`uvx tend@latest check` to validate branch protection, secrets, and bot access.

Check for workflows using `anthropics/claude-code-action`:

```bash
grep -rl 'anthropics/claude-code-action' .github/workflows/ 2>/dev/null
```

If found, delete them — tend replaces claude-code-action entirely. Remind the
user that team members should @-mention the bot account instead of `@claude`.

## 3. Branch protection

Check existing rulesets — skip if one already protects the default branch:

```bash
gh api "repos/$REPO/rulesets" --jq '.[] | {name, enforcement}'
```

If none exist, create a ruleset restricting pushes/merges to the default
branch. Only admins can bypass — the bot (write role) cannot merge.

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

- `type: update` — restricts who can push to or merge into the branch
- `actor_id: 5` = Repository Admin role
- `bypass_mode: exempt` — silently skips the rule for admins

## 4. Create skill overlay (recommended)

Create `.claude/skills/running-tend/SKILL.md` with tend-specific project
guidance. This skill is loaded by tend workflows alongside the generic
`tend-*` skills.

**Do NOT duplicate CLAUDE.md** and **do NOT invent project conventions.**

Ask the user whether they have tend-specific preferences that differ
from defaults. Examples of things that vary between projects:

- PR title format (e.g., conventional commits, Jira ticket prefix)
- Labels the bot should apply to its PRs
- Review request routing (specific teams or people)
- Target branch if not the default branch
- Optional nightly actions (e.g., changelog maintenance — specify file and branch)

If the user has preferences, add them. Otherwise create a placeholder:

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

Show the user the rendered badge and ask before inserting. Place it near
the top of the README — after the title/heading but before the first
paragraph. If there are already badges on that line, append to the same
line.

If no README exists, skip this step.

## 6. Bot account

```bash
gh api users/<bot-name> --jq '.login,.id' 2>/dev/null && echo "EXISTS" || echo "NOT FOUND"
```

If the account doesn't exist:

1. If the user hasn't chosen a name yet, check availability of candidates
   using `gh api users/<name>` (404 = available). Suggest options.
2. Navigate Chrome to `https://github.com/signup`. The user must create the
   account themselves (account creation is a prohibited action for Claude).
3. If a verification code is needed, use the `/jean-claude` skill to
   search Gmail: `/jean-claude gmail search "from:github subject:code" -n 1`
4. After confirmation, re-verify via API.

## 7. Claude OAuth token

An OAuth access token from Claude's auth service — uses the user's Claude
subscription (Max/Team) for billing. Not an API key from console.anthropic.com.

```bash
gh secret list --repo "$REPO" --json name --jq '.[].name' | grep -q CLAUDE_CODE_OAUTH_TOKEN && echo "SET" || echo "NOT SET"
```

If not set, obtain the token via `${CLAUDE_SKILL_DIR}/scripts/oauth-token.sh`
(OAuth 2.0 PKCE flow, opens browser, token valid for 1 year):

```bash
TOKEN=$("${CLAUDE_SKILL_DIR}/scripts/oauth-token.sh")
echo "$TOKEN" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "$REPO"
```

## 8. Bot token and secret

The bot's token needs scopes `repo`, `workflow`, `notifications`,
`write:discussion`, `gist`, and `user`. `workflow` pushes commits that
modify `.github/workflows/` files; `notifications` reads/dismisses the
bot's own threads; `write:discussion` posts on GitHub Discussions; `gist`
lets skills like `review-reviewers` store evidence in the bot's secret
gists; `user` lets step 10 set the bio via `PATCH /user`.

### Default: gh OAuth token

Have the user run, in any terminal:

```bash
gh auth login --hostname github.com --git-protocol https --web \
  --scopes repo,workflow,notifications,write:discussion,gist,user
```

`gh` prints a one-time code and the URL `https://github.com/login/device`.
The user opens that URL in any browser logged in as the bot (Chrome,
Firefox, anything), pastes the code, and authorizes. gh stores the
resulting token in keyring as the active account.

If gh already has the bot in keyring (e.g. expanding scopes on an
existing setup), substitute `gh auth refresh -s ...` — same effect,
preserves the existing token entry.

Switch gh back to the maintainer (whose token has admin on the repo) and
copy the bot's token to the secret:

```bash
gh auth switch --user <maintainer>
gh auth token --user <bot-name> | gh secret set BOT_TOKEN --repo "$REPO"
```

The bot's token stays in keyring — steps 9 and 10 retrieve it via
`GH_TOKEN=$(gh auth token --user <bot-name>)` rather than carrying the
literal value forward.

### Alternative: classic PAT with no expiration

OAuth tokens follow gh's auth lifecycle. For a long-lived token that
never rotates, mint a classic PAT manually in any browser logged in as
the bot:

1. Open `https://github.com/settings/tokens/new?scopes=repo,workflow,notifications,write:discussion,gist,user&description=tend-ci`
2. Set expiration to "No expiration", click "Generate token".
3. Copy the token, store it in gh's keyring under the bot's account so
   later steps can retrieve it the same way as the OAuth path, then
   switch back and set the repo secret:

```bash
echo "<pat-value>" | gh auth login --hostname github.com --with-token
gh auth switch --user <maintainer>
gh auth token --user <bot-name> | gh secret set BOT_TOKEN --repo "$REPO"
```

### Alternative: fine-grained PAT

Fine-grained PATs work but are more cumbersome — no URL pre-fill, the
repo must be selected manually, each scope is a separate permission:
`contents:write`, `pull-requests:write`, `issues:write`, `actions:write`,
`workflows:write`, `discussions:write`, `notifications:write`,
`gists:write`, plus the account-level `Profile: read and write`
permission for the bio.

After creating the token, store it in gh's keyring under the bot's
account so steps 9 and 10 can retrieve it via `gh auth token --user
<bot-name>` the same way as the OAuth and classic-PAT paths, then
switch back and set the repo secret:

```bash
echo "<pat-value>" | gh auth login --hostname github.com --with-token
gh auth switch --user <maintainer>
gh auth token --user <bot-name> | gh secret set BOT_TOKEN --repo "$REPO"
```

### Verify

```bash
gh secret list --repo "$REPO"
```

## 9. Grant bot access

All invitation acceptance in this step uses the bot's token from step 8 via
`GH_TOKEN=$(gh auth token --user <bot-name>)` to authenticate as the bot.

Add the bot as a repo collaborator with write access. GitHub may grant
access directly (204) without creating an invitation — only accept if
one exists:

```bash
BOT_GH_TOKEN=$(gh auth token --user <bot-name>)
gh api "repos/$REPO/collaborators/<bot-name>" -X PUT -f permission=push
INVITE_ID=$(GH_TOKEN=$BOT_GH_TOKEN gh api "user/repository_invitations" --jq ".[] | select(.repository.full_name == \"$REPO\") | .id")
if [ -n "$INVITE_ID" ]; then
  GH_TOKEN=$BOT_GH_TOKEN gh api "user/repository_invitations/$INVITE_ID" -X PATCH
fi
gh api "repos/$REPO/collaborators" --jq '.[].login'
```

## 10. Bot profile bio

Capture what the creator is comfortable with contributors/users asking the
bot to do, then reflect that stance in the bot's profile bio (≤160 chars)
so it's discoverable on the bot's user page. This is advisory — the bot
doesn't gate behavior on it.

Ask the creator which stance applies. Each option is a drop-in bio suffix;
the middle one is the recommended default.

- `tend agent for <owner>/<repo>. Feel free to ask me questions about <repo>.`
- `tend agent for <owner>/<repo>. I triage issues and help maintain <repo>.` (recommended)
- `tend agent for <owner>/<repo>. I respond to maintainers of <repo>.`

Check the current bio as the bot — skip if already set to the chosen value:

```bash
GH_TOKEN=$(gh auth token --user <bot-name>) gh api user --jq '.bio'
```

Otherwise write it (requires `user` scope on the bot's token from step 8):

```bash
GH_TOKEN=$(gh auth token --user <bot-name>) gh api user -X PATCH -f bio="<drafted bio>"
```

## 11. Commit and push

Stage all changes:

```bash
git add .
```

Commit with co-author attribution. Do NOT push without explicit permission.

## Summary checklist

After completing all steps, present this checklist:

- [ ] Config: `.config/tend.toml` created
- [ ] Workflows: generated in `.github/workflows/`
- [ ] Ruleset: merge restriction on default branch, admin bypass
- [ ] Skill overlay: `.claude/skills/running-tend/SKILL.md` (tend-specific only)
- [ ] Badge: offered to add to README (optional)
- [ ] Bot account: `<bot-name>` exists on GitHub
- [ ] Claude token: `CLAUDE_CODE_OAUTH_TOKEN` secret set
- [ ] Bot token: `BOT_TOKEN` secret set with `repo`+`workflow`+`notifications`+`write:discussion`+`gist`+`user` scopes (gh OAuth, classic PAT, or fine-grained)
- [ ] Bot access: repo collaborator with write access, invitation accepted
- [ ] Bot bio: profile bio reflects the authorization stance
- [ ] Committed (push requires explicit permission)
