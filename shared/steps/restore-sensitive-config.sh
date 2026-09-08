#!/usr/bin/env bash
# On a PR, the head checkout contains attacker-controlled files that the CLI
# reads at startup BEFORE any permission gating — SessionStart hooks, env-var
# overrides (NODE_OPTIONS, LD_PRELOAD, PATH), MCP servers, apiKeyHelper shell
# commands. Restore them from the PR base branch, which a maintainer reviewed
# and merged. Used by both harness actions.
#
# The root list is claude-code-action's restore-config.ts set
# (src/github/operations/restore-config.ts) minus the instruction files, which
# lib/pin-instruction-paths.sh covers at every depth for both harnesses. The
# PR's own versions stay readable at `git show HEAD:<path>`; nothing copies
# them anywhere, since a copy made here runs as the runner user and would
# follow a fork-planted symlink into files the agent must never see, such as
# the checkout credential in .git/config.
#
# Known limitation: a PR that legitimately edits .claude/ or CLAUDE.md will have
# those edits reverted for the duration of this run. Same tradeoff
# claude-code-action makes — narrow UX cost for closing the RCE surface.
#
# Invoked by prepare_agent_workspace.py against the runner-owned disposable
# clone before its ownership handoff. That content-ingress bottleneck selects
# and verifies TEND_CONFIG_BASE_SHA and disables inherited Git configuration,
# so this script does not re-derive topology, need a credential, or execute a
# runner-configured filter.
#
# Input (env): TEND_CONFIG_BASE_SHA. Empty means this is not a PR worktree.
set -eo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/pin-instruction-paths.sh
. "${SCRIPT_DIR}/lib/pin-instruction-paths.sh"

SENSITIVE=(.mcp.json .claude.json .gitmodules .ripgreprc .husky)

if [ -z "${TEND_CONFIG_BASE_SHA:-}" ]; then
  echo "No PR base commit selected; nothing to restore"
  exit 0
fi

pin_to_base "$TEND_CONFIG_BASE_SHA" "${SENSITIVE[@]}" "${INSTRUCTION_PATHSPECS[@]}"
