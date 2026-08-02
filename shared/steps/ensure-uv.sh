#!/usr/bin/env bash
# Ensure the runner has uv/uvx available. The runner's own uv runs the
# mitmproxy that backs credential isolation (setup-sandbox.sh does
# `uvx --from mitmproxy …`); the agent gets a separate uv in its sandbox home.
# Shared verbatim by both harness actions.
#
# Inputs (env): GITHUB_PATH (from Actions), HOME.
set -eo pipefail

if command -v uvx >/dev/null 2>&1; then
  echo "uvx already available at $(command -v uvx)"
  exit 0
fi
echo "uvx not found; installing uv"
curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --quiet
echo "$HOME/.local/bin" >> "$GITHUB_PATH"
