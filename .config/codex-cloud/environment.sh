#!/usr/bin/env bash
set -euo pipefail

WORKTRUNK_VERSION=0.73.0
if ! command -v wt >/dev/null 2>&1 ||
  [[ "$(wt --version)" != "wt v${WORKTRUNK_VERSION}" ]]; then
  curl --proto '=https' --tlsv1.2 -LsSf \
    "https://github.com/max-sixty/worktrunk/releases/download/v${WORKTRUNK_VERSION}/worktrunk-installer.sh" |
    WORKTRUNK_UNMANAGED_INSTALL="$HOME/.local/bin" sh
fi

# Approve every command `.config/wt.toml` declares, read back out of Worktrunk
# rather than copied here — a hand-kept list blocks every unattended session in
# the container one `wt.toml` edit later, and re-reviewing buys nothing when the
# container runs this script from that same branch. `wt config approvals add` is
# the review flow and refuses a non-TTY even with `--yes`, so write the file
# directly, reading everything before the redirect truncates it.
WORKTRUNK_PROJECT=$(wt config show --format json | jq -r '.project.identifier | @json')
WORKTRUNK_COMMANDS=$(wt config approvals list --format=json |
  jq -r '.commands[] | "  \(.template | @json),"')
WORKTRUNK_APPROVALS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/worktrunk"
install -d "$WORKTRUNK_APPROVALS_DIR"
cat >"$WORKTRUNK_APPROVALS_DIR/approvals.toml" <<EOF
[projects.$WORKTRUNK_PROJECT]
approved-commands = [
$WORKTRUNK_COMMANDS
]
EOF

# Worktrunk parses what we just generated, so a release that changes the
# approvals schema fails here rather than at the first hook of a Cloud session.
if [[ "$(wt config approvals list --format=json | jq -r '.state')" != approved ]]; then
  echo "Worktrunk approvals do not cover .config/wt.toml" >&2
  wt config approvals list >&2
  exit 1
fi

uv sync --frozen
npm ci --prefix site --prefer-offline --no-audit --no-fund
npm ci --prefix worker --prefer-offline --no-audit --no-fund

uv tool install pre-commit
