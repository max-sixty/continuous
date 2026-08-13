#!/usr/bin/env bash
set -euo pipefail

WORKTRUNK_VERSION=0.71.0
if ! command -v wt >/dev/null 2>&1 ||
  [[ "$(wt --version)" != "wt v${WORKTRUNK_VERSION}" ]]; then
  curl --proto '=https' --tlsv1.2 -LsSf \
    "https://github.com/max-sixty/worktrunk/releases/download/v${WORKTRUNK_VERSION}/worktrunk-installer.sh" |
    WORKTRUNK_UNMANAGED_INSTALL="$HOME/.local/bin" sh
fi

WORKTRUNK_PROJECT=$(wt config show --format json | jq -r '.project.identifier | @json')
install -d "$HOME/.config/worktrunk"
cat >"$HOME/.config/worktrunk/approvals.toml" <<EOF
[projects.$WORKTRUNK_PROJECT]
approved-commands = [
  "wt step copy-ignored",
  "cd site && npm install --prefer-offline --no-audit --no-fund",
  "cd site && npm run dev -- --port {{ branch | hash_port }}",
  "lsof -ti :{{ branch | hash_port }} -sTCP:LISTEN | xargs kill 2>/dev/null || true",
  "dev/test.sh {{ args }}",
]
EOF

uv sync --directory generator --frozen
npm ci --prefix site --prefer-offline --no-audit --no-fund
npm ci --prefix worker --prefer-offline --no-audit --no-fund

uv tool install pre-commit
