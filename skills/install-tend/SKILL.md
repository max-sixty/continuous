---
name: install-tend
description: Sets up tend (Claude-powered CI) on a GitHub repo. Creates config, generates workflows, configures secrets and branch protection via API, guides bot account and PAT creation via browser. Use when setting up tend on a new repo or when asked to install/configure tend.
argument-hint: "[bot-name]"
---

# Install Tend

Set up tend on the current repo.

**Bot name:** $ARGUMENTS (or ask the user if not provided)

Follow each step in order. Skip steps that are already done.

## 1. Prerequisites

Verify before proceeding:

```bash
gh auth status
git remote -v
```

- `gh` must be authenticated
- Repo must have a GitHub remote (need owner/repo for API calls)

Derive `OWNER/REPO` from the remote for later steps:

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
```

## 2. Create config

Create `.config/tend.toml`:

```toml
bot_name = "<bot-name>"
```

Ask the user about overrides. Only add what differs from defaults:

- **Secret names** — default: `BOT_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`
- **Setup steps** — build tools, caches (`[setup]` section)
- **Workflow overrides** — disable workflows, custom cron, watched workflows
  for ci-fix (default watches `"ci"`)
- **Default branch** — only needed if not `main`

## 3. Generate workflows

```bash
uvx tend init
```

Verify 6 workflow files appear in `.github/workflows/tend-*.yaml`.

## 4. Bot account

Check if the bot account exists:

```bash
gh api users/<bot-name> --jq '.login,.id' 2>/dev/null && echo "EXISTS" || echo "NOT FOUND"
```

If it doesn't exist, the user must create it. Open Chrome to the signup page:

```
Navigate to: https://github.com/signup
```

Tell the user: "Create the bot account `<bot-name>` in this browser tab, then
tell me when it's done." Wait for confirmation, then verify:

```bash
gh api users/<bot-name> --jq '.login,.id'
```

## 5. Claude OAuth token

The `CLAUDE_CODE_OAUTH_TOKEN` is an OAuth access token from Claude's auth
service. It uses the user's Claude subscription (Max/Team) for billing —
this is NOT an API key from console.anthropic.com.

Check if already set:

```bash
gh secret list --repo OWNER/REPO --json name --jq '.[].name' | grep -q CLAUDE_CODE_OAUTH_TOKEN && echo "SET" || echo "NOT SET"
```

If not set, obtain the token using the `${CLAUDE_SKILL_DIR}/scripts/oauth-token.sh`
script. This runs a standard OAuth 2.0 PKCE flow against claude.ai and
prints the access token:

```bash
TOKEN=$("${CLAUDE_SKILL_DIR}/scripts/oauth-token.sh")
echo "$TOKEN" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo OWNER/REPO
```

The script opens a browser for the user to sign in with their Claude account.
The token is valid for 1 year.

## 6. Bot PAT and secret

The bot account needs a classic PAT with `repo` scope for GitHub API access.
This must be done while logged in as the bot.

Open Chrome to the token creation page:

```
Navigate to: https://github.com/settings/tokens/new?scopes=repo&description=tend-ci
```

Tell the user: "Log in as `<bot-name>` and generate the token. Copy the token
value and paste it here when ready."

Once the user provides the PAT, set it as a repo secret:

```bash
echo "<pat-value>" | gh secret set BOT_TOKEN --repo OWNER/REPO
```

Verify both secrets exist:

```bash
gh secret list --repo OWNER/REPO
```

## 7. Branch protection

Check existing rulesets first — skip if one already protects the default branch:

```bash
gh api "repos/$REPO/rulesets" --jq '.[] | {name, enforcement, target}'
```

If none exist, create a "Restrict updates" ruleset. This blocks all pushes
and merges to the default branch — only admins can bypass. The bot (write
role) cannot merge regardless of review status.

The merge restriction itself is the security boundary — not required reviews.
Required reviews create problems for solo maintainers (CODEOWNERS deadlock)
and are unnecessary when only admins can merge.

```bash
cat > /tmp/tend-ruleset.json << 'EOF'
{
  "name": "Merge access",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["~DEFAULT_BRANCH"],
      "exclude": []
    }
  },
  "rules": [
    {
      "type": "update"
    }
  ],
  "bypass_actors": [
    {
      "actor_id": 5,
      "actor_type": "RepositoryRole",
      "bypass_mode": "exempt"
    }
  ]
}
EOF

REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
gh api "repos/$REPO/rulesets" --method POST --input /tmp/tend-ruleset.json
```

- `type: update` — restricts who can push to or merge into the branch
- `actor_id: 5` = Repository Admin role
- `bypass_mode: exempt` — silently skips the rule for admins (no checkbox)
- The bot account must have **write** access (not admin)

## 8. Add bot as collaborator

The bot needs write access to push branches and create PRs:

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
gh api "repos/$REPO/collaborators/<bot-name>" -X PUT -f permission=push
```

The bot must accept the invitation. Open Chrome:

```
Navigate to: https://github.com/notifications
```

Tell the user: "Log in as `<bot-name>` and accept the repository invitation,
then tell me when done."

Skip if the bot is already a member of the org that owns the repo.

## 9. Commit and push

Stage only the generated files:

```bash
git add .config/tend.toml .github/workflows/tend-*.yaml
```

Commit with co-author attribution. Do NOT push without explicit permission.

## Summary checklist

After completing all steps, present this checklist:

- [ ] Config: `.config/tend.toml` created
- [ ] Workflows: 6 files in `.github/workflows/`
- [ ] Bot account: `<bot-name>` exists on GitHub
- [ ] Claude token: `CLAUDE_CODE_OAUTH_TOKEN` set (via OAuth script)
- [ ] Bot PAT: `BOT_TOKEN` set (classic PAT with `repo` scope)
- [ ] Ruleset: "Restrict updates" on default branch, admin bypass (exempt mode)
- [ ] Bot access: write collaborator, invitation accepted
- [ ] Committed and pushed
