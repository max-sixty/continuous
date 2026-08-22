#!/usr/bin/env bash
# On a PR, the head checkout contains attacker-controlled files that the CLI
# reads at startup BEFORE any permission gating — SessionStart hooks, env-var
# overrides (NODE_OPTIONS, LD_PRELOAD, PATH), MCP servers, apiKeyHelper shell
# commands. Restore them from the PR base branch, which a maintainer reviewed
# and merged. Used by the Claude harness action.
#
# Path list and ordering mirror claude-code-action's restore-config.ts
# (src/github/operations/restore-config.ts). Delete first (so an
# attacker-controlled .gitmodules can't stall the fetch on credential prompts),
# then fetch base, then restore. Instruction files at any depth are restored
# along with the root list; their pathspecs and the reconcile live in
# lib/pin-instruction-paths.sh, shared with the Codex harness. The PR's own
# versions stay readable at `git show HEAD:<path>`; nothing copies them
# anywhere, since a copy made here runs as the runner user and would follow a
# fork-planted symlink into files the agent must never see, such as the
# checkout credential in .git/config.
#
# Known limitation: a PR that legitimately edits .claude/ or CLAUDE.md will have
# those edits reverted for the duration of this run. Same tradeoff
# claude-code-action makes — narrow UX cost for closing the RCE surface.
#
# Runs before the credential-isolation handoff: it needs the git credential
# actions/checkout persisted, which setup-sandbox.sh strips.
#
# Inputs (env): GITHUB_TOKEN (for gh), GITHUB_EVENT_NAME, GITHUB_EVENT_PATH
# (from Actions).
set -eo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/pin-instruction-paths.sh
. "${SCRIPT_DIR}/lib/pin-instruction-paths.sh"

SENSITIVE=(.claude .mcp.json .claude.json .gitmodules .ripgreprc CLAUDE.md CLAUDE.local.md AGENTS.md .husky)

case "$GITHUB_EVENT_NAME" in
  pull_request_target|pull_request_review|pull_request_review_comment)
    BASE_REF=$(jq -r '.pull_request.base.ref' "$GITHUB_EVENT_PATH")
    ;;
  issue_comment)
    PR_URL=$(jq -r '.issue.pull_request.url // empty' "$GITHUB_EVENT_PATH")
    if [ -z "$PR_URL" ]; then
      echo "issue_comment on issue (not PR); nothing to restore"
      exit 0
    fi
    BASE_REF=$(gh api "${PR_URL#https://api.github.com/}" --jq '.base.ref')
    ;;
  *)
    echo "Event $GITHUB_EVENT_NAME is not a PR event; nothing to restore"
    exit 0
    ;;
esac

if [ -z "$BASE_REF" ] || [ "$BASE_REF" = "null" ]; then
  echo "::warning::Could not determine base ref; skipping config restoration"
  exit 0
fi

echo "Restoring ${SENSITIVE[*]} from origin/$BASE_REF"

# Delete BEFORE fetch so attacker-controlled .gitmodules can't stall on
# credential prompts (git's default fetch.recurseSubmodules=on-demand).
for p in "${SENSITIVE[@]}"; do
  rm -rf "$p"
done

git fetch origin "$BASE_REF" --depth=1 --no-recurse-submodules

pin_to_base "origin/$BASE_REF" "${SENSITIVE[@]}" "${INSTRUCTION_PATHSPECS[@]}"
