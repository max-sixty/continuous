---
name: install-tend
description: Sets up tend (Claude-powered CI) on a GitHub repo. Creates config, generates workflows, configures secrets and branch protection via API, creates bot account and PAT via Chrome. Use when setting up tend on a new repo or when asked to install/configure tend.
---

# Install Tend

@README.md for config options and available settings.

Set up tend on the current repo. Ask the user for the bot name if not provided.

Follow each step in order. Skip steps that are already done — check each
prerequisite before acting. Derive `REPO` once at the start:

```bash
gh auth status
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
```

## Chrome automation

Steps 6 and 8 require Chrome (account creation, PAT generation).
Use Chrome automation tools for these:

1. Call `tabs_context_mcp` to connect
2. Create a tab or reuse an existing one
3. Navigate, interact with forms, and verify outcomes

If Chrome is unavailable, fall back to giving the user URLs and waiting for
confirmation.

For any step where the browser must be logged in as the bot account, verify
the logged-in user by clicking the avatar menu and checking the username
before proceeding.

## 1. Create config

Create `.config/tend.toml` with at minimum `bot_name`. See README.md for all
available config sections (`[secrets]`, `[setup]`, `[workflows.*]`).

```toml
bot_name = "<bot-name>"
```

Check whether the repo already has a bot PAT secret under a non-default name:

```bash
gh secret list --repo "$REPO" --json name --jq '.[].name'
```

If a PAT-like secret exists (e.g., `GH_BOT_TOKEN`, `ROBOT_PAT`), suggest
overriding the default name rather than creating a duplicate:

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

## 2. Generate workflows

```bash
uvx tend@latest init
```

Verify workflow files appear in `.github/workflows/tend-*.yaml`. Run
`uvx tend@latest check` to validate branch protection, secrets, and bot access.

## 2b. Remove existing claude-code-action workflows

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
3. If a verification code is needed, use jean-claude to search for the
   GitHub email: `jean-claude gmail search "from:github subject:code" -n 1`
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

## 8. Bot PAT and secret

The bot needs a classic PAT with `repo`, `workflow`, `notifications`, and
`write:discussion` scopes. `workflow` is required to push commits that modify
`.github/workflows/` files. `notifications` lets the bot read/dismiss its own
notifications. `write:discussion` allows commenting on GitHub Discussions.
Fine-grained PATs also work (`contents:write`, `pull-requests:write`,
`issues:write`, `workflows:write`, `discussions:write`,
`notifications:read`) — create one manually
and skip to step 9. Use Chrome for classic PATs:

1. Verify the browser is logged in as `<bot-name>` (click avatar, check
   username). If not, tell the user to log in as the bot first.
2. Navigate to
   `https://github.com/settings/tokens/new?scopes=repo,workflow,notifications,write:discussion&description=tend-ci`
3. The URL pre-fills the note and scopes. Set expiration to
   "No expiration" via the dropdown.
4. Click "Generate token" (scroll to bottom of page).
5. Read the token from the resulting page using `get_page_text`.
6. Set as repo secret (use the configured secret name from config, default
   `BOT_TOKEN`):

```bash
echo "<pat-value>" | gh secret set BOT_TOKEN --repo "$REPO"
```

Keep the PAT value — step 9 uses it to accept invitations as the bot.

Verify both secrets exist:

```bash
gh secret list --repo "$REPO"
```

## 9. Grant bot access

All invitation acceptance in this step uses the bot's PAT from step 8 via
`GH_TOKEN=<bot-pat>` to authenticate as the bot.

First, check whether the repo belongs to a GitHub organization:

```bash
gh api "repos/$REPO" --jq '.owner.type'
```

**Organization repos:** The bot must be an **org member**, not just an outside
collaborator. Outside collaborators have been observed to get empty
`secrets.BOT_TOKEN` when their actions trigger workflows (e.g., the bot
submits a review, firing `tend-mention`), causing the verify step to fail
with exit code 4.

Check whether the bot is already an org member:

```bash
gh api "orgs/<org>/members/<bot-name>" && echo "ALREADY MEMBER" || echo "NOT MEMBER"
```

If not a member, invite and accept. The invite requires org admin
access — if the user lacks it, ask them to have an org admin invite the
bot manually, then accept via the API call below:

```bash
gh api "orgs/<org>/memberships/<bot-name>" -X PUT -f role=member
GH_TOKEN=<bot-pat> gh api "user/memberships/orgs/<org>" -X PATCH -f state=active
gh api "orgs/<org>/members/<bot-name>" && echo "MEMBER" || echo "NOT MEMBER"
```

Then grant write access to the repo (org members don't automatically get
repo access). For org members, GitHub may grant access directly (204)
without creating an invitation — only accept if one exists:

```bash
gh api "repos/$REPO/collaborators/<bot-name>" -X PUT -f permission=push
INVITE_ID=$(GH_TOKEN=<bot-pat> gh api "user/repository_invitations" --jq ".[] | select(.repository.full_name == \"$REPO\") | .id")
if [ -n "$INVITE_ID" ]; then
  GH_TOKEN=<bot-pat> gh api "user/repository_invitations/$INVITE_ID" -X PATCH
fi
gh api "repos/$REPO/collaborators" --jq '.[].login'
```

**Personal repos:** Add as a repo collaborator and accept:

```bash
gh api "repos/$REPO/collaborators/<bot-name>" -X PUT -f permission=push
INVITE_ID=$(GH_TOKEN=<bot-pat> gh api "user/repository_invitations" --jq ".[] | select(.repository.full_name == \"$REPO\") | .id")
if [ -n "$INVITE_ID" ]; then
  GH_TOKEN=<bot-pat> gh api "user/repository_invitations/$INVITE_ID" -X PATCH
fi
gh api "repos/$REPO/collaborators" --jq '.[].login'
```

## 10. Commit and push

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
- [ ] Bot PAT: `BOT_TOKEN` secret set (classic `repo`+`workflow`+`notifications`+`write:discussion` or fine-grained)
- [ ] Bot access: org member (org repos) or repo collaborator (personal repos), invitation accepted
- [ ] Committed (push requires explicit permission)
