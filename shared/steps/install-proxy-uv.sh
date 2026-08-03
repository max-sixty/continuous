#!/usr/bin/env bash
# Install the pinned uv that runs the credential-injection proxy
# (setup-sandbox.sh launches mitmdump through it).
#
# Deliberately isolated: it installs into a tend-owned dir and never touches
# $GITHUB_PATH, so it can neither be shadowed by nor shadow the adopter's own
# uv. That matters twice over. The binary that launches the process holding
# the real PAT and model credential is now deterministic — pinned like
# `claude_version` and `mitmproxy_version`, rather than whatever the adopter's
# `setup:` happened to leave on PATH, or whatever astral shipped that morning.
# And nothing tend installs competes with the version an adopter's `uv.toml`
# pins for their own steps.
#
# Inputs (env): UV_VERSION, TEND_UV_DIR.
set -euo pipefail

url="https://astral.sh/uv/${UV_VERSION}/install.sh"
# Retry transient CDN failures — every workflow now depends on this install,
# where the previous "only if uv is missing" step short-circuited on most repos.
for i in 1 2 3; do
  if timeout 60 bash -c "set -o pipefail; curl -LsSf '$url' \
    | env UV_INSTALL_DIR='$TEND_UV_DIR' UV_NO_MODIFY_PATH=1 sh -s -- --quiet"; then
    break
  fi
  if [ "$i" = 3 ]; then
    echo "::error::failed to install uv ${UV_VERSION} after 3 attempts"
    exit 1
  fi
  echo "uv install attempt $i failed; retrying"
  sleep $((i * 5))
done

"${TEND_UV_DIR}/uvx" --version
