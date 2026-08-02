#!/usr/bin/env bash
# Install the tend plugins into the sandbox's own ~/.claude. The marketplace is
# this action's checkout; the runner copies just the manifest + plugins (a small
# markdown tree, NOT the toolchain) into a sandbox-owned dir so the sandbox can
# read it regardless of where Actions placed the action. Then the sandbox
# installs from there, as the sandbox user, with the shared agent env
# ($AGENT_ENV_FILE: proxy routing, CA trust, dummy credentials) — if `claude`
# validates auth it reaches api.anthropic.com via the injecting proxy; the real
# secret never enters the sandbox. `claude plugin install` is non-interactive
# (CLAUDE_CODE_REMOTE=1 suppresses prompts), so it needs no onboarding pre-seed.
# Used by the Claude harness action.
#
# Inputs (env): MARKETPLACE_ROOT (dir containing .claude-plugin/ and plugins/;
# differs per action by checkout depth), SANDBOX, AGENT_HOME, AGENT_ENV_FILE
# (exported by setup-sandbox.sh via $GITHUB_ENV).
set -eo pipefail

MARKETPLACE_SRC="$(realpath "$MARKETPLACE_ROOT")"
SANDBOX_MKT="$AGENT_HOME/tend-marketplace"
sudo rm -rf "$SANDBOX_MKT"
sudo mkdir -p "$SANDBOX_MKT"
sudo cp -a "$MARKETPLACE_SRC/.claude-plugin" "$MARKETPLACE_SRC/plugins" "$SANDBOX_MKT/"
sudo chown -R "${SANDBOX}:${SANDBOX}" "$SANDBOX_MKT"

mapfile -t AGENT_ENV <"$AGENT_ENV_FILE"
sudo -u "$SANDBOX" env "${AGENT_ENV[@]}" \
  bash -c '
    set -euo pipefail
    claude plugin marketplace add "$1"
    claude plugin install install-tend@tend
    claude plugin install tend-ci-runner@tend
    claude plugin list 2>/dev/null || true
  ' _ "$SANDBOX_MKT"
