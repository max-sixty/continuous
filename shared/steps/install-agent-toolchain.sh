#!/usr/bin/env bash
# Install the agent toolchain (uv + the claude binary) DIRECTLY into the
# sandbox user's home, so there is no ~200 MB `cp -a` from the runner. The
# claude binary carries absolute-path references into ~/.local/share, so it must
# be installed in place — installing as runner and moving breaks it. uv is the
# agent's own (its skills run `uvx tend@latest …`); the runner has a separate uv
# for the proxy. Installers fetch over the direct network (no proxy env here).
# Used by the Claude harness action.
#
# Inputs (env): CLAUDE_VERSION (claude binary version), SANDBOX and AGENT_HOME
# (exported by setup-sandbox.sh via $GITHUB_ENV).
set -eo pipefail

# XDG_* pinned under the sandbox home: the runner exports
# XDG_CONFIG_HOME=/home/runner/.config (leaks through sudo), and uv
# writes its receipt to $XDG_CONFIG_HOME/uv — unwritable by the
# sandbox UID. Pin all four base dirs so the installers (and any
# XDG-aware tool) land under $AGENT_HOME. Install fetches go direct
# (no proxy env here), so this step does not source $AGENT_ENV_FILE.
# UV_NO_MODIFY_PATH=1 keeps uv's installer from appending source lines
# to the sandbox's shell profiles — the launch env sets PATH explicitly,
# so a profile edit is inert noise.
sudo -u "$SANDBOX" env HOME="$AGENT_HOME" CLAUDE_VERSION="$CLAUDE_VERSION" \
  XDG_CONFIG_HOME="$AGENT_HOME/.config" \
  XDG_CACHE_HOME="$AGENT_HOME/.cache" \
  XDG_DATA_HOME="$AGENT_HOME/.local/share" \
  XDG_STATE_HOME="$AGENT_HOME/.local/state" \
  UV_NO_MODIFY_PATH=1 \
  bash <<'EOF'
set -euo pipefail
# One retry mechanism for both installer CDNs — transient 403s/5xxs
# are the shared failure class. The inner `set -o pipefail` is
# required: without it a curl failure passes empty stdin to the
# downstream `bash -s --`, which exits 0, masking the failure so the
# loop breaks after one attempt without retrying.
fetch_install() {
  for i in 1 2 3; do
    if timeout 60 bash -c "set -o pipefail; curl -fsSL $1 | bash -s -- $2"; then
      return 0
    fi
    echo "Install attempt $i for $1 failed; retrying"
    sleep $((i * 5))
  done
  return 1
}
fetch_install https://astral.sh/uv/install.sh --quiet
fetch_install https://claude.ai/install.sh "$CLAUDE_VERSION"
EOF
if ! sudo -u "$SANDBOX" test -x "$AGENT_HOME/.local/bin/claude"; then
  echo "::error::claude binary not found at $AGENT_HOME/.local/bin/claude after install"
  exit 1
fi
sudo -u "$SANDBOX" "$AGENT_HOME/.local/bin/claude" --version
