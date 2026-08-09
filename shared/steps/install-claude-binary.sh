#!/usr/bin/env bash
# Install the claude binary DIRECTLY into the sandbox user's home, so there is
# no ~200 MB `cp -a` from the runner. It carries absolute-path references into
# ~/.local/share, so it must be installed in place — installing as runner and
# moving breaks it. The installer fetches over the direct network (no proxy env
# here). Used by the Claude harness action.
#
# Only claude: nothing here provisions a language toolchain. A uv installed at
# $AGENT_HOME/.local/bin would sit ahead of the adopter's own on the sandbox
# PATH, overriding the version their uv.toml pins. Skills that need `uvx
# tend@...` go through plugins/tend-ci-runner/scripts/tend-uvx.sh, which keeps
# its uv off PATH; the proxy has a separate pinned one
# (shared/steps/install-proxy-uv.sh).
#
# Inputs (env): CLAUDE_VERSION (claude binary version), SANDBOX and AGENT_HOME
# (exported by setup-sandbox.sh via $GITHUB_ENV).
set -eo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Retry transient 403s/5xxs from the installer CDN, on the window lib/retry.sh
# holds for the pre-agent installers. The lib is concatenated onto the front
# of the sandbox script rather than sourced from inside it: nothing grants the
# sandbox UID read access to the action's own checkout — setup-sandbox.sh
# grants traversal (o+x) on the workspace's ancestors only — whereas stdin is a
# stream the runner, not the sandbox, reads the file into.
#
# XDG_* pinned under the sandbox home: the runner exports
# XDG_CONFIG_HOME=/home/runner/.config (leaks through sudo), which the
# sandbox UID can't write. Pin all four base dirs so the installer (and
# any XDG-aware tool the agent later runs) lands under $AGENT_HOME.
# Install fetches go direct (no proxy env here), so this step does not
# source $AGENT_ENV_FILE.
cat "${SCRIPT_DIR}/lib/retry.sh" - <<'EOF' \
  | sudo -u "$SANDBOX" env HOME="$AGENT_HOME" CLAUDE_VERSION="$CLAUDE_VERSION" \
      XDG_CONFIG_HOME="$AGENT_HOME/.config" \
      XDG_CACHE_HOME="$AGENT_HOME/.cache" \
      XDG_DATA_HOME="$AGENT_HOME/.local/share" \
      XDG_STATE_HOME="$AGENT_HOME/.local/state" \
      bash
set -euo pipefail
retry_install "claude $CLAUDE_VERSION" \
  "curl -fsSL https://claude.ai/install.sh | bash -s -- '$CLAUDE_VERSION'"
EOF
if ! sudo -u "$SANDBOX" test -x "$AGENT_HOME/.local/bin/claude"; then
  echo "::error::claude binary not found at $AGENT_HOME/.local/bin/claude after install"
  exit 1
fi
sudo -u "$SANDBOX" "$AGENT_HOME/.local/bin/claude" --version
