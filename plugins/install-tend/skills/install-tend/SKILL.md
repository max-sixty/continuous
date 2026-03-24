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

Steps 3, 5, and 7 require a browser (account creation, PAT generation,
invitation acceptance). Use Chrome automation tools for all of these:

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

Ask the user about other overrides (setup steps, workflow overrides, default
branch).

## 2. Generate workflows

```bash
uvx tend init
```

Verify workflow files appear in `.github/workflows/tend-*.yaml`. Run
`uvx tend check` to validate branch protection, secrets, and bot access.

## 2b. Remove existing claude-code-action workflows

Check for workflows using `anthropics/claude-code-action`:

```bash
grep -rl 'anthropics/claude-code-action' .github/workflows/ 2>/dev/null
```

If found, delete them — tend replaces claude-code-action entirely. Remind the
user that team members should @-mention the bot account instead of `@claude`.

## 3. Bot account

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

## 4. Claude OAuth token

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

## 5. Bot PAT and secret

The bot needs a classic PAT with `repo` scope. Use Chrome:

1. Verify the browser is logged in as `<bot-name>` (click avatar, check
   username). If not, tell the user to log in as the bot first.
2. Navigate to
   `https://github.com/settings/tokens/new?scopes=repo&description=tend-ci`
3. The URL pre-fills the note and `repo` scope. Set expiration to
   "No expiration" via the dropdown.
4. Click "Generate token" (scroll to bottom of page).
5. Read the token from the resulting page using `get_page_text`.
6. Set as repo secret (use the configured secret name from config, default
   `BOT_TOKEN`):

```bash
echo "<pat-value>" | gh secret set BOT_TOKEN --repo "$REPO"
```

Verify both secrets exist:

```bash
gh secret list --repo "$REPO"
```

## 6. Branch protection

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

## 7. Add bot as collaborator

```bash
gh api "repos/$REPO/collaborators/<bot-name>" -X PUT -f permission=push
```

The bot must accept the invitation. Use Chrome:

1. Navigate to `https://github.com/<owner>/<repo>/invitations` (not
   `/notifications` — invitations don't appear there for new accounts).
2. Click "Accept invitation".
3. Verify via API:

```bash
gh api "repos/$REPO/collaborators" --jq '.[].login'
```

Skip if the bot is already a member of the org that owns the repo.

## 8. Create skill overlay (recommended)

Create `.claude/skills/running-tend/SKILL.md` with tend-specific project
guidance. This skill is loaded by tend workflows alongside the generic
`tend-*` skills.

**Do NOT duplicate CLAUDE.md.** The overlay should only contain information
that tend workflows need beyond what CLAUDE.md already provides:

- PR title conventions (prefix format, scope rules)
- CI workflow names (which workflow tend-ci-fix watches)
- Automerge behavior (which bot PRs get auto-merged)
- Dependency management (Dependabot vs Renovate, tend-renovate enabled/disabled)

Build commands, test commands, error conventions, code style, and project
structure belong in CLAUDE.md — tend reads CLAUDE.md like any other Claude
session.

## 9. Commit and push

Stage only the generated files:

```bash
git add .config/tend.toml .github/workflows/tend-*.yaml .claude/skills/running-tend/
```

Also stage any setup actions created for tend (e.g., `.github/actions/tend-setup/`).

Commit with co-author attribution. Do NOT push without explicit permission.

## Summary checklist

After completing all steps, present this checklist:

- [ ] Config: `.config/tend.toml` created
- [ ] Workflows: generated in `.github/workflows/`
- [ ] Bot account: `<bot-name>` exists on GitHub
- [ ] Claude token: `CLAUDE_CODE_OAUTH_TOKEN` secret set
- [ ] Bot PAT: `BOT_TOKEN` secret set (classic PAT, `repo` scope)
- [ ] Ruleset: merge restriction on default branch, admin bypass
- [ ] Bot access: write collaborator, invitation accepted
- [ ] Skill overlay: `.claude/skills/running-tend/SKILL.md` (tend-specific only)
- [ ] Committed (push requires explicit permission)
