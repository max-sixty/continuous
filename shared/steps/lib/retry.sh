#!/usr/bin/env bash
# The retry window the pre-agent installers share: install-claude-binary.sh,
# install-proxy-uv.sh and install-codex-cli.sh each reach a third party — two
# CDNs and the npm registry — before the agent step exists, so a blip that
# exhausts their retries costs the whole run: the step goes red having done
# none of the work the trigger asked for. The lost run is what justifies a
# window this wide, independent of how the failure is reported afterwards.
#
# Sourced, not executed.

# retry_install LABEL COMMAND — run COMMAND under a 60s timeout, up to five
# times, backing off 5/10/20/40s plus jitter. Jittered because a matrix
# workflow's legs install concurrently from one runner's egress address: a
# rate limit hits them together, and an unjittered backoff has them retry
# together too.
#
# The inner `set -o pipefail` is required by the `curl | sh` callers:
# without it a curl failure passes empty stdin to the downstream shell,
# which exits 0, masking the failure so the loop breaks after one attempt
# without retrying.
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
