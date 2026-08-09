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

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/retry.sh
. "${SCRIPT_DIR}/lib/retry.sh"

url="https://astral.sh/uv/${UV_VERSION}/install.sh"
# Retry transient CDN failures — every workflow now depends on this install,
# where the previous "only if uv is missing" step short-circuited on most repos.
retry_install "uv ${UV_VERSION}" "curl -LsSf '$url' \
  | env UV_INSTALL_DIR='$TEND_UV_DIR' UV_NO_MODIFY_PATH=1 sh -s -- --quiet"

"${TEND_UV_DIR}/uvx" --version
