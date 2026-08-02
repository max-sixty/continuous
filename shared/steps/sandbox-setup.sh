#!/usr/bin/env bash
# Run the adopter's `sandbox_setup:` commands (from .config/tend.yaml, threaded
# in as TEND_SANDBOX_SETUP) INSIDE the sandbox, as the non-sudo sandbox user,
# after the toolchain and plugins are installed and just before the agent runs.
# This is the general lever runner-side `setup:` can't provide: `setup:` runs as
# the runner user around the composite action and never reaches the sandbox
# env. Commands run with the same launch env the agent gets ($AGENT_ENV_FILE:
# proxy routing, CA trust, dummy credentials, plus any sandbox_path/sandbox_env
# additions) and with the workspace as the working directory.
#
# Env-only tweaks (PATH, exported vars) do NOT persist to the agent from here —
# a child shell's exports die with it. Use `sandbox_path:` / `sandbox_env:` for
# those; use `sandbox_setup:` for actions with on-disk effects (installing a
# tool, warming a cache, generating a file).
#
# Inputs (env): TEND_SANDBOX_SETUP (the commands; empty → no-op), SANDBOX and
# AGENT_ENV_FILE (exported by setup-sandbox.sh via $GITHUB_ENV). Used by the
# Claude harness action.
set -euo pipefail

[ -n "${TEND_SANDBOX_SETUP:-}" ] || exit 0

# Run as the sandbox user with the agent's launch env. The commands go through
# `bash -c`'s argument: no temp file (so no sandbox-side read permission on a
# runner-owned path), and not stdin (so a setup command that reads stdin — an
# installer prompt, `read` — can't swallow the remaining lines and exit 0). `-e`
# inside so a failing setup command fails the step loudly rather than silently
# proceeding to the run.
mapfile -t AGENT_ENV <"$AGENT_ENV_FILE"
sudo -u "$SANDBOX" env "${AGENT_ENV[@]}" bash -eo pipefail -c "$TEND_SANDBOX_SETUP"
echo "[sandbox-setup] ran adopter sandbox_setup commands as $SANDBOX"
