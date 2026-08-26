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
  local agent_path path_entry
  set_inputs
  # The workspace path leads; a literal `~` exercises expansion against the
  # sandbox home. The configured directory may be populated later.
  # shellcheck disable=SC2088
  export TEND_SANDBOX_PATH="$GITHUB_WORKSPACE/.tend-explicit/bin"$'\n~/.tend-tilde/bin'
  # `sandbox_env` reserves the credential and routing names, not the GITHUB_*
  # context, so this entry is accepted and lands in $AGENT_ENV_FILE. That the
  # real workflow name then beats it is lib/sandbox-launch-env.sh's
  # postcondition, unit-tested there; what this adds is the whole path — a
  # config value threaded through setup-sandbox.sh into the file, composed by
  # the lib, landing in a real sandbox under a real uid.
  export TEND_SANDBOX_ENV="GITHUB_WORKFLOW=spoofed-by-sandbox-env"
  MITMPROXY_VERSION=$(yq -e '.inputs.mitmproxy_version.default' claude/action.yaml)
  export MITMPROXY_VERSION
  UV_VERSION=$(yq -e '.inputs.uv_version.default' claude/action.yaml) \
    bash shared/steps/install-proxy-uv.sh
  bash proxy/setup-sandbox.sh | tee "$RUNNER_TEMP/setup.log"

  agent_path=$(sed -n 's/^\[setup-sandbox\] sandbox PATH: //p' "$RUNNER_TEMP/setup.log")
  test -n "$agent_path"
  while IFS= read -r path_entry; do
    case "$path_entry" in
      "$GITHUB_WORKSPACE" | "$GITHUB_WORKSPACE"/*) ;;
      "$HOME" | "$HOME"/*)
        echo "::error::a non-workspace runner-home entry reached the sandbox PATH: $path_entry"
        exit 1
        ;;
    esac
  done < <(tr : '\n' <<<"$agent_path")
  case "$agent_path" in
    "$GITHUB_WORKSPACE/.tend-explicit/bin":*) ;;
    *) echo "::error::sandbox_path did not lead the PATH: $agent_path"; exit 1 ;;
  esac
  case ":$agent_path:" in
    *:/home/tend-sandbox/.tend-seeded/bin:*) ;;
    *) echo "::error::sandbox-owned counterpart omitted from PATH: $agent_path"; exit 1 ;;
  esac
  case ":$agent_path:" in
    *:/home/tend-sandbox/.tend-tilde/bin:*) ;;
    *) echo "::error::sandbox_path ~ was not expanded: $agent_path"; exit 1 ;;
  esac
  grep -q 'runner-home PATH entries unavailable in sandbox:' "$RUNNER_TEMP/setup.log"
  grep -q 'runner-home commands blocked from shared fallbacks:.*tend-probe' \
    "$RUNNER_TEMP/setup.log"
  rm "$HOME/.cargo-install/tend-probe/bin/sudo"
}

verify() {
  local blocked_output rc report setup_commands dummy_token
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

  # Both halves of the set lib/sandbox-launch-env.sh defines, asserted from
  # inside the sandbox. `test -n` on the carried names so an empty value fails
  # here rather than passing the grep below vacuously; GITHUB_ENV for the
  # withheld ones, non-vacuous because Actions sets it on the runner. The env
  # file's dummy token must survive a real one on the runner, so this call
  # supplies a distinct value and the sandbox asserts it still sees the file's.
  dummy_token=$(sed -n 's/^GITHUB_TOKEN=//p' "$AGENT_ENV_FILE")
  test -n "$dummy_token"
  test -n "$GITHUB_ENV"
  setup_commands=$(printf '%s\n' \
    'mkdir -p ~/.local/bin' \
    'cp ~runner/.cargo-install/tend-probe/bin/tend-probe ~/.local/bin/' \
    'tend-probe' \
    'test -n "$GITHUB_WORKFLOW"' \
    'test -n "$GITHUB_EVENT_NAME"' \
    'test -z "${GITHUB_ENV:-}"' \
    "test \"\$GITHUB_TOKEN\" = \"$dummy_token\"" \
    'echo "sandbox_setup workflow: $GITHUB_WORKFLOW"')
  GITHUB_TOKEN=runner-token-must-not-cross \
    TEND_SANDBOX_SETUP="$setup_commands" \
    bash shared/steps/sandbox-setup.sh | tee "$RUNNER_TEMP/report-after.log"
  if grep 'unavailable to the agent:' "$RUNNER_TEMP/report-after.log" | grep -q tend-probe; then
    echo "::error::sandbox_setup did not close the tool gap"
    exit 1
  fi
  test "$(sudo -u "$SANDBOX" env "${agent_env[@]}" tend-probe)" = probe
  grep -qF "sandbox_setup workflow: $GITHUB_WORKFLOW" "$RUNNER_TEMP/report-after.log"
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
    /usr/bin/sudo chown -R "$(id -u):$(id -g)" "$GITHUB_WORKSPACE"
  fi
  /usr/bin/sudo rm -f /usr/local/bin/tend-probe "$shared/tend-shared"
  /usr/bin/sudo rmdir "$shared" "${shared%/bin}" 2>/dev/null || true
  /usr/bin/sudo rm -f /etc/skel/.tend-seeded/bin/tend-seeded
  /usr/bin/sudo rmdir /etc/skel/.tend-seeded/bin \
    /etc/skel/.tend-seeded 2>/dev/null || true
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
