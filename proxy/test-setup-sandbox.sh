#!/usr/bin/env bash
# Hosted-runner integration test for setup-sandbox.sh. Each command is a
# separate Actions step because GITHUB_PATH and GITHUB_ENV take effect only in
# subsequent steps.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

set_test_inputs() {
  export TEND_GH_TOKEN=dummy
  export TEND_ANTHROPIC_OAUTH_TOKEN=dummy
  export ACTION_PATH="$repo_root"
  export TEND_UV_DIR="${RUNNER_TEMP}/tend-uv"
  export UV_CACHE_DIR="${RUNNER_TEMP}/tend-mitmproxy-uv"
}

plant() {
  local bin relative sdk workspace_bin runner_alias inode_alias
  bin="$HOME/.cargo-install/tend-probe/bin"
  relative="$RUNNER_TEMP/tend-node/node_modules"
  sdk="$HOME/.sdkman/candidates/java"
  workspace_bin="$GITHUB_WORKSPACE/.tend-probe/bin"
  mkdir -p "$bin" "$HOME/.local/bin" "$HOME/.local/lib/tend" \
    "$HOME/.cargo/bin" "$relative/.bin" "$relative/tend-relative/bin" \
    "$sdk/17/bin" "$workspace_bin"
  printf '#!/bin/sh\necho probe\n' >"$bin/tend-probe"
  printf '#!/bin/sh\necho private\n' >"$bin/tend-private"
  printf '#!/bin/sh\necho writable\n' >"$bin/tend-writable"
  printf '#!/bin/sh\necho runner-private\n' >"$bin/tend-fallback"
  # shellcheck disable=SC2016
  printf '#!/bin/sh\ncat "$HOME/.local/lib/tend/value"\n' >"$HOME/.local/bin/tend-local"
  printf 'runner-library\n' >"$HOME/.local/lib/tend/value"
  printf '#!/bin/sh\necho pinned\n' >"$HOME/.cargo/bin/tend-pinned"
  printf 'registry-token = "must-not-cross"\n' >"$HOME/.cargo/credentials.toml"
  printf '[registries.private]\ntoken = "must-not-cross"\n' >"$HOME/.cargo/config.toml"
  mkdir -p "$HOME/.local/share/tend-private"
  printf 'must-not-cross\n' >"$HOME/.local/share/tend-private/token"
  mkdir -p "$HOME/.local/share/uv/tools/tend-tool"
  printf '[tool.options]\nindex = [{ url = "https://token@example.invalid" }]\n' \
    >"$HOME/.local/share/uv/tools/tend-tool/uv-receipt.toml"
  chmod 644 "$HOME/.cargo/credentials.toml" "$HOME/.cargo/config.toml" \
    "$HOME/.local/share/tend-private/token" \
    "$HOME/.local/share/uv/tools/tend-tool/uv-receipt.toml"
  printf '#!/bin/sh\necho relative\n' >"$relative/tend-relative/bin/tend-relative"
  ln -s ../tend-relative/bin/tend-relative "$relative/.bin/tend-relative"
  printf '#!/bin/sh\necho alias\n' >"$sdk/17/bin/tend-alias"
  ln -s 17 "$sdk/current"
  printf '#!/bin/sh\necho workspace\n' >"$workspace_bin/tend-workspace"
  printf '#!/bin/sh\necho system-fallback\n' | \
    sudo tee /usr/local/bin/tend-fallback >/dev/null
  cp /bin/true "$bin/tend-setid"
  cp /bin/true "$bin/tend-capability"
  runner_alias="/tmp/tend-runner-home-alias-$GITHUB_RUN_ID"
  inode_alias="/tmp/tend-runner-inode-alias-$GITHUB_RUN_ID"
  ln -s "$bin" "$runner_alias"
  ln -s "$bin/tend-setid" "$inode_alias"
  ln -s "$inode_alias" "$bin/tend-link-bypass"
  mkfifo "$bin/tend-fifo"
  chmod 755 "$bin/tend-probe" "$HOME/.local/bin/tend-local" \
    "$HOME/.cargo/bin/tend-pinned" "$relative/tend-relative/bin/tend-relative" \
    "$sdk/17/bin/tend-alias" "$workspace_bin/tend-workspace" \
    "$bin/tend-capability"
  sudo chmod 755 /usr/local/bin/tend-fallback
  chmod 700 "$bin/tend-private" "$bin/tend-fallback"
  chmod 777 "$bin/tend-writable"
  chmod 4755 "$bin/tend-setid"
  sudo setcap cap_net_bind_service=ep "$bin/tend-capability"
  {
    echo "$bin"
    echo "$relative/.bin"
    echo "$sdk/current/bin"
    echo "$workspace_bin"
    echo "$runner_alias"
  } >>"$GITHUB_PATH"
  sudo install -d /etc/skel/.cargo/bin
  printf '#!/bin/sh\necho stale\n' | sudo tee /etc/skel/.cargo/bin/tend-pinned >/dev/null
  sudo chmod 755 /etc/skel/.cargo/bin/tend-pinned
}

setup() {
  local agent_path path_entry
  local -a agent_path_entries
  set_test_inputs
  # The production input expands this literal tilde to the sandbox home.
  # shellcheck disable=SC2088
  export TEND_SANDBOX_PATH='~/.cargo/bin'
  MITMPROXY_VERSION=$(yq -e '.inputs.mitmproxy_version.default' claude/action.yaml)
  export MITMPROXY_VERSION
  UV_VERSION=$(yq -e '.inputs.uv_version.default' claude/action.yaml) \
    bash shared/steps/install-proxy-uv.sh
  bash proxy/setup-sandbox.sh | tee "$RUNNER_TEMP/setup.log"

  agent_path=$(sed -n 's/^\[setup-sandbox\] sandbox PATH: //p' "$RUNNER_TEMP/setup.log")
  if [ -z "$agent_path" ]; then
    echo "::error::setup-sandbox.sh logged no sandbox PATH"
    exit 1
  fi
  IFS=: read -ra agent_path_entries <<<"$agent_path"
  for path_entry in "${agent_path_entries[@]}"; do
    case "$path_entry" in
      "$GITHUB_WORKSPACE" | "$GITHUB_WORKSPACE"/*) ;;
      "$HOME" | "$HOME"/*)
        echo "::error::a non-workspace runner-home entry reached the sandbox PATH: $path_entry"
        exit 1
        ;;
    esac
  done
  case "$agent_path" in
    /home/tend-sandbox/.cargo/bin:*) ;;
    *)
      echo "::error::sandbox_path's ~ entry did not expand to the front: $agent_path"
      exit 1
      ;;
  esac
  case ":$agent_path:" in
    *":/tmp/tend-runner-home-alias-$GITHUB_RUN_ID:"*)
      echo "::error::an external alias exposed its runner-home target: $agent_path"
      exit 1
      ;;
  esac
}

verify() {
  local source_bin report
  local -a agent_env
  source_bin="$HOME/.cargo-install/tend-probe/bin"
  mapfile -t agent_env <"$AGENT_ENV_FILE"
  if grep -q '^$' "$AGENT_ENV_FILE"; then
    echo "::error::the agent env file contains an empty argv element"
    exit 1
  fi
  sudo -u "$SANDBOX" env "${agent_env[@]}" bash -euc '
    test "$(tend-probe)" = probe
    test "$(tend-local)" = runner-library
    test "$(tend-pinned)" = pinned
    test "$(tend-relative)" = relative
    test "$(tend-alias)" = alias
    test "$(tend-workspace)" = workspace
    test "$(tend-fallback)" = system-fallback
    hatch --version
    tend-setid
    tend-capability
    tend-link-bypass
    case "$(rustc --version)" in "rustc 1.85.0 "*) ;; *) exit 1 ;; esac
    test "$(readlink -f "$JAVA_HOME")" = "$1"
    test ! -e ~/.cargo-install/tend-probe/bin/tend-private
    test ! -e ~/.cargo-install/tend-probe/bin/tend-writable
    test ! -e ~/.cargo-install/tend-probe/bin/tend-fifo
    test ! -e ~/.cargo/credentials.toml
    test ! -e ~/.cargo/config.toml
    test ! -e ~/.local/share/tend-private
    test ! -e ~/.local/share/uv/tools/tend-tool/uv-receipt.toml
    test ! -e ~/.local/share/uv/tools/hatch/uv-receipt.toml
    test ! -w ~/.cargo-install/tend-probe/bin/tend-probe
    for command in tend-setid tend-capability; do
      mode=$(stat -c %a ~/.cargo-install/tend-probe/bin/$command)
      test $((8#$mode & 06000)) -eq 0
    done
  ' _ "$(readlink -f "$JAVA_HOME")"
  if test "$source_bin/tend-probe" -ef \
      /home/tend-sandbox/.cargo-install/tend-probe/bin/tend-probe; then
    echo "::error::the sandbox tool still shares the runner's inode"
    exit 1
  fi
  if test "$source_bin/tend-setid" -ef \
      /home/tend-sandbox/.cargo-install/tend-probe/bin/tend-link-bypass; then
    echo "::error::a copied symlink still reaches its runner-owned inode"
    exit 1
  fi
  test -n "$(getcap "$source_bin/tend-capability")"
  test -z "$(getcap /home/tend-sandbox/.cargo-install/tend-probe/bin/tend-capability)"
  grep -q "runner-selected commands not mirrored into sandbox: .*tend-fallback" \
    "$RUNNER_TEMP/setup.log"

  bash shared/steps/sandbox-setup.sh | tee "$RUNNER_TEMP/report.log"
  report=$(grep "not the agent's:" "$RUNNER_TEMP/report.log" || true)
  case "$report" in
    *tend-private*tend-writable*) ;;
    *) echo "::error::the report did not name both refused tools: $report"; exit 1 ;;
  esac
  case "$report" in
    *tend-probe* | *tend-local* | *tend-pinned* | *" git "*)
      echo "::error::the report named a reachable command: $report"
      exit 1
      ;;
  esac
}

verify_sandbox_setup() {
  local setup_commands
  setup_commands=$'printf \'#!/bin/sh\\necho private\\n\' >~/.local/bin/tend-private\nchmod +x ~/.local/bin/tend-private\ntend-private'
  TEND_SANDBOX_SETUP="$setup_commands" \
    bash shared/steps/sandbox-setup.sh | tee "$RUNNER_TEMP/report-after.log"
  if grep "not the agent's:" "$RUNNER_TEMP/report-after.log" | grep -q tend-private; then
    echo "::error::still unreachable after sandbox_setup installed it"
    exit 1
  fi
}

verify_refusals() {
  local alias rc empty_rc
  set_test_inputs
  export MITMPROXY_VERSION=0
  alias="/tmp/tend-runner-home-alias-$GITHUB_RUN_ID"
  case "$alias" in
    "$HOME" | "$HOME"/*)
      echo "::error::the canonical-refusal fixture is lexically under HOME"
      exit 1
      ;;
  esac
  TEND_SANDBOX_PATH="$alias" \
    bash proxy/setup-sandbox.sh >"$RUNNER_TEMP/refused.log" 2>&1 && rc=0 || rc=$?
  sed 's/^::error::/refused: /' "$RUNNER_TEMP/refused.log"
  if [ "${rc:-0}" -eq 0 ]; then
    echo "::error::a runner-home sandbox_path entry was accepted"
    exit 1
  fi
  grep -q "::error::sandbox_path entry .* is under the runner's home" \
    "$RUNNER_TEMP/refused.log"

  GITHUB_WORKSPACE='' bash proxy/setup-sandbox.sh \
    >"$RUNNER_TEMP/empty-workspace.log" 2>&1 && empty_rc=0 || empty_rc=$?
  if [ "${empty_rc:-0}" -eq 0 ]; then
    echo "::error::an empty GITHUB_WORKSPACE was accepted"
    exit 1
  fi
  grep -q "::error::GITHUB_WORKSPACE must name" \
    "$RUNNER_TEMP/empty-workspace.log"
}

cleanup() {
  if [ -n "${SANDBOX:-}" ]; then
    sudo chown -R "$(id -u):$(id -g)" "$GITHUB_WORKSPACE"
  fi
}

case "${1:-}" in
  plant) plant ;;
  setup) setup ;;
  verify) verify ;;
  verify-sandbox-setup) verify_sandbox_setup ;;
  verify-refusals) verify_refusals ;;
  cleanup) cleanup ;;
  *)
    echo "usage: $0 {plant|setup|verify|verify-sandbox-setup|verify-refusals|cleanup}" >&2
    exit 2
    ;;
esac
