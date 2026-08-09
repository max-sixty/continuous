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

# XDG_* pinned under the sandbox home: the runner exports
# XDG_CONFIG_HOME=/home/runner/.config (leaks through sudo), which the
# sandbox UID can't write. Pin all four base dirs so the installer (and
# any XDG-aware tool the agent later runs) lands under $AGENT_HOME.
# Install fetches go direct (no proxy env here), so this step does not
# source $AGENT_ENV_FILE.
sudo -u "$SANDBOX" env HOME="$AGENT_HOME" CLAUDE_VERSION="$CLAUDE_VERSION" \
  XDG_CONFIG_HOME="$AGENT_HOME/.config" \
  XDG_CACHE_HOME="$AGENT_HOME/.cache" \
  XDG_DATA_HOME="$AGENT_HOME/.local/share" \
  XDG_STATE_HOME="$AGENT_HOME/.local/state" \
  bash <<'EOF'
set -euo pipefail
# Retry transient 403s/5xxs from the installer CDN. The inner
# `set -o pipefail` is required: without it a curl failure passes empty
# stdin to the downstream `bash -s --`, which exits 0, masking the
# failure so the loop breaks after one attempt without retrying.
#
# Five attempts backing off exponentially, not three at a flat 5s. The old
# window spent all three attempts inside ~15s, which is short enough for one
# CDN blip to take the whole run: the step goes red, and because `Report
# failure` is gated on the agent step, a failure this early leaves no outage
# row either, so the run disappears silently.
#
# Jittered, because the legs of a matrix workflow install concurrently from
# one runner's egress address. A rate limit hits them together, and an
# unjittered backoff has them retry together too — every leg reproducing the
# burst that tripped it. The jitter spreads the retries out.
ATTEMPTS=5
for i in $(seq 1 "$ATTEMPTS"); do
  if timeout 60 bash -c "set -o pipefail; \
    curl -fsSL https://claude.ai/install.sh | bash -s -- '$CLAUDE_VERSION'"; then
    break
  fi
  if [ "$i" -eq "$ATTEMPTS" ]; then
    echo "::error::failed to install claude $CLAUDE_VERSION after $ATTEMPTS attempts"
    exit 1
  fi
  BACKOFF=$((5 * 2 ** (i - 1) + RANDOM % 10))
  echo "Install attempt $i failed; retrying in ${BACKOFF}s"
  sleep "$BACKOFF"
done
EOF
if ! sudo -u "$SANDBOX" test -x "$AGENT_HOME/.local/bin/claude"; then
  echo "::error::claude binary not found at $AGENT_HOME/.local/bin/claude after install"
  exit 1
fi
sudo -u "$SANDBOX" "$AGENT_HOME/.local/bin/claude" --version
