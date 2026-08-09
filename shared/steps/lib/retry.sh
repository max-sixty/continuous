#!/usr/bin/env bash
# The retry window the pre-agent installers share: install-claude-binary.sh
# and install-proxy-uv.sh both fetch from a CDN before the agent step exists,
# so a blip that exhausts their retries loses the run with no outage row
# (`Report failure` is gated on the agent step's own outcome).
#
# Sourced, not executed.

# retry_install LABEL COMMAND — run COMMAND under a 60s timeout, up to five
# times, backing off 5/10/20/40s plus jitter. Jittered because a matrix
# workflow's legs install concurrently from one runner's egress address: a
# rate limit hits them together, and an unjittered backoff has them retry
# together too.
#
# The inner `set -o pipefail` is required by both callers' `curl | sh`
# shape: without it a curl failure passes empty stdin to the downstream
# shell, which exits 0, masking the failure so the loop breaks after one
# attempt without retrying.
retry_install() {
  local label=$1 cmd=$2 attempts=5 i backoff
  for i in $(seq 1 "$attempts"); do
    if timeout 60 bash -c "set -o pipefail; $cmd"; then
      return 0
    fi
    if [ "$i" -eq "$attempts" ]; then
      echo "::error::failed to install ${label} after ${attempts} attempts"
      return 1
    fi
    backoff=$((5 * 2 ** (i - 1) + RANDOM % 10))
    echo "${label} install attempt $i failed; retrying in ${backoff}s"
    sleep "$backoff"
  done
}
