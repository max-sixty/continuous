#!/usr/bin/env bash
# Hosted-runner integration test for the sandbox UID and PATH boundary. Commands
# are separate Actions steps because GITHUB_PATH/GITHUB_ENV affect only later
# steps.
set -euo pipefail

set_inputs() {
  export TEND_GH_TOKEN=dummy
  export TEND_ANTHROPIC_OAUTH_TOKEN=dummy
  export ACTION_PATH="$GITHUB_WORKSPACE"
  export TEND_UV_DIR="$RUNNER_TEMP/tend-uv"
  export UV_CACHE_DIR="$RUNNER_TEMP/tend-mitmproxy-uv"
}

plant() {
  local bin seeded shared workspace_explicit workspace_path
  bin="$HOME/.cargo-install/tend-probe/bin"
  seeded="$HOME/.tend-seeded/bin"
  shared="/opt/tend-sandbox-test-$GITHUB_RUN_ID/bin"
  workspace_explicit="$GITHUB_WORKSPACE/.tend-explicit/bin"
  workspace_path="$GITHUB_WORKSPACE/.tend-path/bin"
  mkdir -p "$bin" "$seeded" "$workspace_explicit" "$workspace_path"
  printf '#!/bin/sh\necho probe\n' >"$bin/tend-probe"
  chmod +x "$bin/tend-probe"
  # useradd copies /etc/skel into an independent sandbox home. A corresponding
  # runner-home PATH entry should resolve to that sandbox-owned copy.
  printf '#!/bin/sh\necho runner-seed\n' >"$seeded/tend-seeded"
  chmod +x "$seeded/tend-seeded"
  sudo install -d -m 755 /etc/skel/.tend-seeded/bin
  printf '#!/bin/sh\necho sandbox-seed\n' \
    | sudo tee /etc/skel/.tend-seeded/bin/tend-seeded >/dev/null
  sudo chmod +x /etc/skel/.tend-seeded/bin/tend-seeded
  printf '#!/bin/sh\necho workspace-explicit\n' \
    >"$workspace_explicit/tend-workspace-explicit"
  printf '#!/bin/sh\necho workspace-path\n' \
    >"$workspace_path/tend-workspace-path"
  chmod +x "$workspace_explicit/tend-workspace-explicit" \
    "$workspace_path/tend-workspace-path"
  # A same-name shared fallback must be blocked, while an unrelated command in
  # a non-base shared directory must cross the boundary unchanged.
  printf '#!/bin/sh\necho system-fallback\n' \
    | sudo tee /usr/local/bin/tend-probe >/dev/null
  sudo chmod +x /usr/local/bin/tend-probe
  sudo install -d -m 755 "$shared"
  printf '#!/bin/sh\necho shared\n' | sudo tee "$shared/tend-shared" >/dev/null
  sudo chmod +x "$shared/tend-shared"
  # setup-sandbox must capture this PATH entry as tool data without resolving
  # its privileged utilities through an adopter-controlled directory.
  printf '#!/bin/sh\nexit 99\n' >"$bin/sudo"
  chmod +x "$bin/sudo"
  echo "$shared" >>"$GITHUB_PATH"
  echo "$workspace_path" >>"$GITHUB_PATH"
  echo "$seeded" >>"$GITHUB_PATH"
  echo "$bin" >>"$GITHUB_PATH"
  ln -s "$bin" "$RUNNER_TEMP/tend-runner-home-alias"
}

setup() {
  local agent_path
  set_inputs
  export TEND_SANDBOX_PATH="$GITHUB_WORKSPACE/.tend-explicit/bin"
  MITMPROXY_VERSION=$(yq -e '.inputs.mitmproxy_version.default' claude/action.yaml)
  export MITMPROXY_VERSION
  UV_VERSION=$(yq -e '.inputs.uv_version.default' claude/action.yaml) \
    bash shared/steps/install-proxy-uv.sh
  bash proxy/setup-sandbox.sh | tee "$RUNNER_TEMP/setup.log"

  agent_path=$(sed -n 's/^\[setup-sandbox\] sandbox PATH: //p' "$RUNNER_TEMP/setup.log")
  test -n "$agent_path"
  case ":$agent_path:" in
    *":$HOME/"* | *":$HOME:"*)
      echo "::error::a runner-home entry reached the sandbox PATH: $agent_path"
      exit 1
      ;;
  esac
  case "$agent_path" in
    "$GITHUB_WORKSPACE/.tend-explicit/bin":*) ;;
    *) echo "::error::sandbox_path did not lead the PATH: $agent_path"; exit 1 ;;
  esac
  case ":$agent_path:" in
    *:/home/tend-sandbox/.tend-seeded/bin:*) ;;
    *) echo "::error::sandbox-owned counterpart omitted from PATH: $agent_path"; exit 1 ;;
  esac
  grep -q 'runner-home PATH entries unavailable in sandbox:' "$RUNNER_TEMP/setup.log"
  grep -q 'runner-home commands blocked from shared fallbacks:.*tend-probe' \
    "$RUNNER_TEMP/setup.log"
  rm "$HOME/.cargo-install/tend-probe/bin/sudo"
}

verify() {
  local blocked_output rc report setup_commands
  local -a agent_env
  mapfile -t agent_env <"$AGENT_ENV_FILE"
  blocked_output=$(sudo -u "$SANDBOX" env "${agent_env[@]}" tend-probe 2>&1) \
    && rc=0 || rc=$?
  test "${rc:-0}" -eq 127
  case "$blocked_output" in
    *'came from the runner home and is unavailable'*) ;;
    *) echo "::error::home tool fell through instead of failing: $blocked_output"; exit 1 ;;
  esac
  test "$(sudo -u "$SANDBOX" env "${agent_env[@]}" tend-shared)" = shared
  test "$(sudo -u "$SANDBOX" env "${agent_env[@]}" tend-seeded)" = sandbox-seed
  test "$(sudo -u "$SANDBOX" env "${agent_env[@]}" tend-workspace-explicit)" = workspace-explicit
  test "$(sudo -u "$SANDBOX" env "${agent_env[@]}" tend-workspace-path)" = workspace-path

  # The dropped runner-home command is reported; shared, workspace, and
  # independently seeded sandbox-home commands are reachable and stay absent.
  bash shared/steps/sandbox-setup.sh | tee "$RUNNER_TEMP/report.log"
  report=$(grep 'unavailable to the agent:' "$RUNNER_TEMP/report.log" || true)
  case "$report" in
    *tend-probe*) ;;
    *) echo "::error::the report omitted the runner-home tool: $report"; exit 1 ;;
  esac
  case "$report" in
    *" git "* | *tend-shared* | *tend-seeded* | *tend-workspace-*)
      echo "::error::the report named a reachable command: $report"
      exit 1
      ;;
  esac

  setup_commands=$'mkdir -p ~/.local/bin\ncp ~runner/.cargo-install/tend-probe/bin/tend-probe ~/.local/bin/\ntend-probe'
  TEND_SANDBOX_SETUP="$setup_commands" \
    bash shared/steps/sandbox-setup.sh | tee "$RUNNER_TEMP/report-after.log"
  if grep 'unavailable to the agent:' "$RUNNER_TEMP/report-after.log" | grep -q tend-probe; then
    echo "::error::sandbox_setup did not close the tool gap"
    exit 1
  fi
  test "$(sudo -u "$SANDBOX" env "${agent_env[@]}" tend-probe)" = probe
}

# An explicit runner-home path is the one route that could bypass the rewrite.
# These re-runs exit before the workspace chown, so they do not disturb verify.
verify_refusals() {
  local rc empty_rc
  set_inputs
  export MITMPROXY_VERSION=0
  TEND_SANDBOX_PATH="$RUNNER_TEMP/tend-runner-home-alias" \
    bash proxy/setup-sandbox.sh >"$RUNNER_TEMP/refused.log" 2>&1 && rc=0 || rc=$?
  # Actions parses workflow commands out of step output; don't annotate this
  # passing refusal test with the error it deliberately provokes.
  sed 's/^::error::/refused: /' "$RUNNER_TEMP/refused.log"
  test "${rc:-0}" -ne 0
  grep -q "::error::sandbox_path entry .* is under the runner's home" \
    "$RUNNER_TEMP/refused.log"

  GITHUB_WORKSPACE='' bash proxy/setup-sandbox.sh \
    >"$RUNNER_TEMP/empty-workspace.log" 2>&1 && empty_rc=0 || empty_rc=$?
  test "${empty_rc:-0}" -ne 0
  grep -q '::error::GITHUB_WORKSPACE must name' "$RUNNER_TEMP/empty-workspace.log"
}

cleanup() {
  local shared
  shared="/opt/tend-sandbox-test-$GITHUB_RUN_ID/bin"
  if [ -n "${SANDBOX:-}" ]; then
    sudo chown -R "$(id -u):$(id -g)" "$GITHUB_WORKSPACE"
  fi
  sudo rm -f /usr/local/bin/tend-probe "$shared/tend-shared"
  sudo rmdir "$shared" "${shared%/bin}" 2>/dev/null || true
  sudo rm -f /etc/skel/.tend-seeded/bin/tend-seeded
  sudo rmdir /etc/skel/.tend-seeded/bin /etc/skel/.tend-seeded 2>/dev/null || true
}

case "${1:-}" in
  plant) plant ;;
  setup) setup ;;
  verify) verify ;;
  verify-refusals) verify_refusals ;;
  cleanup) cleanup ;;
  *)
    echo "usage: $0 {plant|setup|verify|verify-refusals|cleanup}" >&2
    exit 2
    ;;
esac
