#!/usr/bin/env bash
set -euo pipefail

WORKTRUNK_VERSION=0.76.0
if ! command -v wt >/dev/null 2>&1 ||
  [[ "$(wt --version)" != "wt v${WORKTRUNK_VERSION}" ]]; then
  curl --proto '=https' --tlsv1.2 -LsSf \
    "https://github.com/max-sixty/worktrunk/releases/download/v${WORKTRUNK_VERSION}/worktrunk-installer.sh" |
    WORKTRUNK_UNMANAGED_INSTALL="$HOME/.local/bin" sh
fi

# Pre-approve the commands `.config/wt.toml` declares, so unattended `wt` runs
# in the container don't block on the prompt. `--yes` is the CI path; review
# buys nothing here, since the container runs this script from that same branch.
wt config approvals add --yes

uv sync --frozen
npm ci --prefix site --prefer-offline --no-audit --no-fund
npm ci --prefix worker --prefer-offline --no-audit --no-fund

uv tool install pre-commit
