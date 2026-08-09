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
#
# Five attempts backing off exponentially, matching install-claude-binary.sh,
# and for the same reasons. Three attempts at a flat 5s spend the whole window
# inside ~15s, which a CDN blip has outlasted; and because this step runs ahead
# of the agent, `Report failure` — gated on the agent step's own outcome —
# files no outage row, so a run lost here leaves no trace.
#
# Jittered, because a matrix workflow's legs install concurrently from one
# runner's egress address. A rate limit hits them together, and an unjittered
# backoff has them retry together too.
ATTEMPTS=5
for i in $(seq 1 "$ATTEMPTS"); do
  if timeout 60 bash -c "set -o pipefail; curl -LsSf '$url' \
    | env UV_INSTALL_DIR='$TEND_UV_DIR' UV_NO_MODIFY_PATH=1 sh -s -- --quiet"; then
    break
  fi
  if [ "$i" -eq "$ATTEMPTS" ]; then
    echo "::error::failed to install uv ${UV_VERSION} after $ATTEMPTS attempts"
    exit 1
  fi
  BACKOFF=$((5 * 2 ** (i - 1) + RANDOM % 10))
  echo "uv install attempt $i failed; retrying in ${BACKOFF}s"
  sleep "$BACKOFF"
done

"${TEND_UV_DIR}/uvx" --version
