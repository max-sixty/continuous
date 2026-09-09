#!/usr/bin/env bash
# Hosted-runner integration test for the sandbox UID and PATH boundary. Commands
# are separate Actions steps because GITHUB_PATH/GITHUB_ENV affect only later
# steps.
set -euo pipefail

set_inputs() {
  export TEND_GH_TOKEN=dummy
  export TEND_ANTHROPIC_OAUTH_TOKEN=dummy
  export ACTION_PATH="$TEND_TEST_ACTION_PATH"
  export TEND_UV_DIR="$RUNNER_TEMP/tend-uv"
  export UV_CACHE_DIR="$RUNNER_TEMP/tend-mitmproxy-uv"
}

plant() {
  local bin seeded shared workspace_explicit workspace_path
  bin="$HOME/.cargo-install/tend-probe/bin"
  seeded="$HOME/.tend-seeded/bin"
  shared="/opt/tend-sandbox-test-$GITHUB_RUN_ID/bin"
  TEND_AGENT_CONTAINER=$(mktemp -d /tmp/tend-agent-workspace-test.XXXXXX)
  TEND_AGENT_WORKSPACE="$TEND_AGENT_CONTAINER/checkout"
  TEND_RUNNER_WORKSPACE="$GITHUB_WORKSPACE"
  TEND_TEST_ACTION_PATH="$TEND_AGENT_CONTAINER/action"
  git clone --no-local --no-hardlinks "$GITHUB_WORKSPACE" "$TEND_AGENT_WORKSPACE"
  chmod 700 "$TEND_AGENT_WORKSPACE"
  chmod 711 "$TEND_AGENT_CONTAINER"
  mkdir -p "$TEND_TEST_ACTION_PATH"
  cp -a "$GITHUB_WORKSPACE/claude" "$GITHUB_WORKSPACE/codex" \
    "$GITHUB_WORKSPACE/proxy" \
    "$GITHUB_WORKSPACE/shared" \
    "$TEND_TEST_ACTION_PATH/"
  workspace_explicit="$TEND_AGENT_WORKSPACE/.tend-explicit/bin"
  workspace_path="$TEND_AGENT_WORKSPACE/.tend-path/bin"
  mkdir -p "$bin" "$seeded" "$workspace_explicit" "$workspace_path"
  printf '#!/bin/sh\necho probe\n' >"$bin/tend-probe"
  printf '#!/bin/sh\necho runner-home-uv\n' >"$bin/uv"
  chmod +x "$bin/tend-probe" "$bin/uv"
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
  printf '#!/bin/sh\necho adopter-uv\n' | sudo tee "$shared/uv" >/dev/null
  sudo chmod +x "$shared/tend-shared" "$shared/uv"
  # setup_sandbox.py must capture this PATH entry as tool data without resolving
  # its privileged utilities through an adopter-controlled directory.
  printf '#!/bin/sh\nexit 99\n' >"$bin/sudo"
  chmod +x "$bin/sudo"
  echo "$shared" >>"$GITHUB_PATH"
  echo "$workspace_path" >>"$GITHUB_PATH"
  echo "$seeded" >>"$GITHUB_PATH"
  echo "$bin" >>"$GITHUB_PATH"
  ln -s "$bin" "$RUNNER_TEMP/tend-runner-home-alias"
  {
    echo "TEND_AGENT_WORKSPACE=$TEND_AGENT_WORKSPACE"
    echo "TEND_AGENT_CONTAINER=$TEND_AGENT_CONTAINER"
    echo "TEND_RUNNER_WORKSPACE=$TEND_RUNNER_WORKSPACE"
    echo "TEND_TEST_ACTION_PATH=$TEND_TEST_ACTION_PATH"
  } >> "$GITHUB_ENV"
}

setup() {
  local action_run agent_path hostile_python hostile_site path_entry
  set_inputs
  # The workspace path leads; a literal `~` exercises expansion against the
  # sandbox home. The configured directory may be populated later.
  # shellcheck disable=SC2088
  export TEND_SANDBOX_PATH="$TEND_AGENT_WORKSPACE/.tend-explicit/bin"$'\n~/.tend-tilde/bin'
  # `sandbox_env` reserves the credential and routing names, not the GITHUB_*
  # context, so this entry is accepted and lands in $AGENT_ENV_FILE. That the
  # real workflow name then beats it is _sandbox.py's
  # postcondition, unit-tested there; what this adds is the whole path — a
  # config value threaded through setup_sandbox.py into the file, composed by
  # the lib, landing in a real sandbox under a real uid.
  export TEND_SANDBOX_ENV="GITHUB_WORKFLOW=spoofed-by-sandbox-env"
  MITMPROXY_VERSION=$(yq -e '.inputs.mitmproxy_version.default' claude/action.yaml)
  export MITMPROXY_VERSION
  UV_VERSION=$(yq -e '.inputs.uv_version.default' claude/action.yaml) \
    UV_INSTALL_DIR="$TEND_UV_DIR" bash shared/steps/install-uv.sh
  # The setup step receives both real credentials. Repository-controlled
  # Python and uv environment variables must not execute code before the
  # runner-owned script has established the sandbox boundary.
  hostile_python="$RUNNER_TEMP/tend-hostile-python"
  hostile_site="$RUNNER_TEMP/tend-hostile-site"
  mkdir -p "$hostile_site"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    "touch '$RUNNER_TEMP/uv-python-used'" \
    'exec /usr/bin/python3 "$@"' >"$hostile_python"
  chmod +x "$hostile_python"
  printf '%s\n' \
    'from pathlib import Path' \
    "Path('$RUNNER_TEMP/pythonpath-used').touch()" \
    >"$hostile_site/sitecustomize.py"
  export UV_PYTHON="$hostile_python"
  export PYTHONPATH="$hostile_site"
  # Exercise the composite action's entrypoint rather than calling the setup
  # script directly. The boundary depends on the PATH the action passes in.
  action_run=$(yq -er '.runs.steps[] | select(.name == "Set up credential-isolation sandbox") | .run' claude/action.yaml)
  action_run=${action_run//'${{ github.action_path }}'/"$TEND_TEST_ACTION_PATH/claude"}
  /usr/bin/bash --noprofile --norc -eo pipefail -c "$action_run" \
    | tee "$RUNNER_TEMP/setup.log"
  test ! -e "$RUNNER_TEMP/uv-python-used"
  test ! -e "$RUNNER_TEMP/pythonpath-used"

  agent_path=$(sed -n 's/^\[setup-sandbox\] sandbox PATH: //p' "$RUNNER_TEMP/setup.log")
  test -n "$agent_path"
  while IFS= read -r path_entry; do
    case "$path_entry" in
      "$TEND_AGENT_WORKSPACE" | "$TEND_AGENT_WORKSPACE"/*) ;;
      "$HOME" | "$HOME"/*)
        echo "::error::a non-workspace runner-home entry reached the sandbox PATH: $path_entry"
        exit 1
        ;;
    esac
  done < <(tr : '\n' <<<"$agent_path")
  case "$agent_path" in
    "$TEND_AGENT_WORKSPACE/.tend-explicit/bin":*) ;;
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

install_agent_uv() {
  local action_run harness private_action
  private_action=$(mktemp -d "$RUNNER_TEMP/tend-private-action.XXXXXX")
  mkdir -p "$private_action/claude" "$private_action/codex" \
    "$private_action/shared/steps"
  cp "$TEND_TEST_ACTION_PATH/shared/steps/install-uv.sh" \
    "$private_action/shared/steps/"
  if sudo -u "$SANDBOX" test -r "$private_action/shared/steps/install-uv.sh"; then
    echo "::error::private action fixture is readable by the sandbox user"
    exit 1
  fi
  UV_VERSION=$(yq -e '.inputs.uv_version.default' claude/action.yaml)
  export UV_VERSION
  for harness in claude codex; do
    action_run=$(yq -er '.runs.steps[] | select(.name == "Install agent uv fallback (sandbox)") | .run' "$harness/action.yaml")
    action_run=${action_run//'${{ github.action_path }}'/"$private_action/$harness"}
    /usr/bin/bash --noprofile --norc -eo pipefail -c "$action_run"
  done
  rm -rf "$private_action"
}

verify() {
  local blocked_output rc
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
  sudo -u "$SANDBOX" test -x "$TEND_AGENT_UV_DIR/uv"
  grep -q "^PATH=.*:${TEND_AGENT_UV_DIR}$" "$AGENT_ENV_FILE"
  test "$(sudo -u "$SANDBOX" env "${agent_env[@]}" uv --version)" = adopter-uv
}

# An explicit runner-home path is the one route that could bypass the rewrite.
# These re-runs exit before the workspace chown, so they do not disturb verify.
verify_refusals() {
  local rc empty_rc
  set_inputs
  export MITMPROXY_VERSION=0
  TEND_SANDBOX_PATH="$RUNNER_TEMP/tend-runner-home-alias" \
    "$TEND_UV_DIR/uv" run --script proxy/setup_sandbox.py \
    >"$RUNNER_TEMP/refused.log" 2>&1 && rc=0 || rc=$?
  # Actions parses workflow commands out of step output; don't annotate this
  # passing refusal test with the error it deliberately provokes.
  sed 's/^::error::/refused: /' "$RUNNER_TEMP/refused.log"
  test "${rc:-0}" -ne 0
  grep -q "::error::sandbox_path entry .* is under the runner's home" \
    "$RUNNER_TEMP/refused.log"

  GITHUB_WORKSPACE='' "$TEND_UV_DIR/uv" run --script proxy/setup_sandbox.py \
    >"$RUNNER_TEMP/empty-workspace.log" 2>&1 && empty_rc=0 || empty_rc=$?
  test "${empty_rc:-0}" -ne 0
  grep -q '::error::GITHUB_WORKSPACE must name' "$RUNNER_TEMP/empty-workspace.log"
}

verify_srt() {
  local claude_argv claude_env claude_stub codex_argv codex_env codex_stub dummy_token
  local github_output private_action probe_info probe_pid probe_port rc runner_summary
  local setup_commands setup_proxy stream_json tool_root
  github_output="$RUNNER_TEMP/srt-github-output"
  runner_summary="$RUNNER_TEMP/srt-step-summary"
  probe_info="$RUNNER_TEMP/srt-network-probe"
  tool_root="$TEND_TEST_ACTION_PATH/probe-bin"
  : > "$github_output"
  : > "$runner_summary"
  mkdir -p "$tool_root"
  printf '#!/bin/sh\necho tend-srt-tool-ok\n' > "$tool_root/probe"
  chmod +x "$tool_root/probe"

  private_action=$(mktemp -d "$RUNNER_TEMP/tend-private-runtime.XXXXXX")
  mkdir -p "$private_action/shared" "$private_action/codex"
  cp -R "$TEND_TEST_ACTION_PATH/shared/steps" "$private_action/shared/"
  cp "$TEND_TEST_ACTION_PATH/codex/runner.py" "$private_action/codex/"
  if sudo -u "$SANDBOX" test -r "$private_action/shared/steps/sandbox_runtime.mjs"; then
    echo "::error::private runtime fixture is readable by the sandbox user"
    exit 1
  fi

  claude_stub="$TEND_AGENT_WORKSPACE/.tend-explicit/bin/claude"
  claude_env="$TEND_AGENT_WORKSPACE/.tend-claude-env"
  claude_argv="$TEND_AGENT_WORKSPACE/.tend-claude-argv"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    "env > '$claude_env'" \
    "printf '%s\\n' \"\$@\" > '$claude_argv'" \
    'sleep 300 &' \
    'printf "%s\n" "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"stub turn\"}]}}"' \
    'printf "%s\n" "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false}"' \
    | sudo -u "$SANDBOX" tee "$claude_stub" >/dev/null
  sudo -u "$SANDBOX" chmod +x "$claude_stub"

  codex_stub="$TEND_AGENT_WORKSPACE/.tend-explicit/bin/codex-stub"
  codex_argv="$TEND_AGENT_WORKSPACE/.tend-codex-argv"
  codex_env="$TEND_AGENT_WORKSPACE/.tend-codex-env"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    "env > '$codex_env'" \
    "printf '%s\\n' \"\$@\" > '$codex_argv'" \
    "curl --fail --silent \"\$CODEX_PROXY_URL\" > '$TEND_AGENT_WORKSPACE/.tend-codex-network'" \
    'tool_no_proxy=' \
    'tool_no_proxy_lower=' \
    'for arg in "$@"; do' \
    '  case "$arg" in' \
    '    shell_environment_policy.set.NO_PROXY=*) tool_no_proxy=${arg#*=} ;;' \
    '    shell_environment_policy.set.no_proxy=*) tool_no_proxy_lower=${arg#*=} ;;' \
    '  esac' \
    'done' \
    'tool_no_proxy=${tool_no_proxy#\"}; tool_no_proxy=${tool_no_proxy%\"}' \
    'tool_no_proxy_lower=${tool_no_proxy_lower#\"}; tool_no_proxy_lower=${tool_no_proxy_lower%\"}' \
    "printf 'tend-srt-local-ok\\n' > '$TEND_AGENT_WORKSPACE/.tend-local-probe'" \
    "local_log='$TEND_AGENT_WORKSPACE/.tend-local-server-log'" \
    "/usr/bin/python3 -u -m http.server 0 --bind 127.0.0.1 --directory '$TEND_AGENT_WORKSPACE' >\"\$local_log\" 2>&1 &" \
    'local_pid=$!' \
    'for _ in {1..50}; do grep -q " port [0-9]" "$local_log" && break; sleep 0.1; done' \
    'local_port=$(sed -n "s/.* port \\([0-9][0-9]*\\) .*/\\1/p" "$local_log")' \
    "NO_PROXY=\"\$tool_no_proxy\" no_proxy=\"\$tool_no_proxy_lower\" curl --fail --silent \"http://127.0.0.1:\$local_port/.tend-local-probe\" > '$TEND_AGENT_WORKSPACE/.tend-codex-local-network'" \
    'kill "$local_pid" 2>/dev/null || true' \
    'while [ "$#" -gt 0 ]; do' \
    '  if [ "$1" = --output-last-message ]; then' \
    '    printf "codex final\n" > "$2"' \
    '    break' \
    '  fi' \
    '  shift' \
    'done' \
    | sudo -u "$SANDBOX" tee "$codex_stub" >/dev/null
  sudo -u "$SANDBOX" chmod +x "$codex_stub"

  /usr/bin/python3 - "$probe_info" <<'PY' &
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"tend-srt-network-ok\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
Path(sys.argv[1]).write_text(str(server.server_port))
server.serve_forever()
PY
  probe_pid=$!
  for _ in {1..50}; do
    [ -s "$probe_info" ] && break
    sleep 0.1
  done
  probe_port=$(cat "$probe_info")

  dummy_token=$(sed -n 's/^GITHUB_TOKEN=//p' "$AGENT_ENV_FILE")
  test -n "$dummy_token"
  setup_commands=$(printf '%s\n' \
    'touch "$TEND_RUNNER_WORKSPACE/.tend-srt-wrote-here" 2>/dev/null || true' \
    'printf "%s\n" "$HTTP_PROXY" > .tend-setup-proxy' \
    'mkdir -p ~/.local/bin' \
    'printf "#!/bin/sh\necho probe\n" > ~/.local/bin/tend-probe' \
    'chmod +x ~/.local/bin/tend-probe' \
    'tend-probe > .tend-setup-tool' \
    'test -z "${GITHUB_ENV:-}"' \
    "test \"\$GITHUB_TOKEN\" = \"$dummy_token\"")

  rm -rf -- "$RUNNER_TEMP/tend-agent-export"
  rc=0
  ACTION_PATH="$private_action" \
    TEND_HARNESS=claude \
    TEND_LIFECYCLE="$private_action/shared/steps/agent_lifecycle.py" \
    TEND_SANDBOX_SETUP="$setup_commands" \
    TEND_BOUNDARY_PROBE_URL="http://127.0.0.1:$probe_port/" \
    TEND_BOUNDARY_PROBE_EXECUTABLE="$tool_root/probe" \
    TEND_MODEL=stub-model TEND_ALLOWED_TOOLS='Bash,Read' \
    TEND_SYSTEM_PROMPT='stub system prompt' TEND_PROMPT='stub prompt' \
    TEND_TIMEOUT_SEC=60 SHOW_FULL_OUTPUT=true \
    BOT_NAME=stub-bot BOT_ID=123 CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 \
    GITHUB_TOKEN=runner-token-must-not-cross \
    GITHUB_OUTPUT="$github_output" \
    GITHUB_STEP_SUMMARY="$runner_summary" \
    /usr/bin/python3 -E -s \
      "$TEND_TEST_ACTION_PATH/shared/steps/launch_sandbox_runtime.py" || rc=$?
  test "$rc" -eq 0
  grep -qx 'sandbox_reaped=true' "$github_output"
  stream_json=$(sed -n 's/^stream_json=//p' "$github_output")
  test -n "$stream_json"
  grep -q '"stub turn"' "$stream_json"
  grep -q 'stub turn' "$runner_summary"
  test ! -e "$TEND_RUNNER_WORKSPACE/.tend-srt-wrote-here"
  test "$(sudo -u "$SANDBOX" cat "$TEND_AGENT_WORKSPACE/.tend-setup-tool")" = probe
  setup_proxy=$(sudo -u "$SANDBOX" cat "$TEND_AGENT_WORKSPACE/.tend-setup-proxy")
  test -n "$setup_proxy"
  test "$setup_proxy" != 'http://127.0.0.1:8899'
  sudo -u "$SANDBOX" grep -qxF "HTTP_PROXY=$setup_proxy" "$claude_env"
  sudo -u "$SANDBOX" grep -qxF "GITHUB_TOKEN=$dummy_token" "$claude_env"
  if sudo -u "$SANDBOX" grep -q '^GITHUB_ENV=' "$claude_env"; then
    echo "::error::runner command-file path crossed into Claude"
    exit 1
  fi
  for want in -p --model stub-model --permission-mode bypassPermissions \
    --allowedTools 'Bash,Read' --append-system-prompt 'stub system prompt' \
    --output-format stream-json --verbose 'stub prompt'; do
    sudo -u "$SANDBOX" grep -qxF -- "$want" "$claude_argv"
  done

  rm -rf -- "$RUNNER_TEMP/tend-agent-export"
  : > "$github_output"
  rc=0
  ACTION_PATH="$private_action" \
    TEND_HARNESS=codex \
    TEND_LIFECYCLE="$private_action/shared/steps/agent_lifecycle.py" \
    TEND_CODEX_RUNNER="$private_action/codex/runner.py" \
    TEND_SANDBOX_SETUP='' \
    TEND_BOUNDARY_PROBE_URL="http://127.0.0.1:$probe_port/" \
    TEND_BOUNDARY_PROBE_EXECUTABLE="$tool_root/probe" \
    TEND_CODEX_ROOT="$TEND_TEST_ACTION_PATH" \
    CODEX_BIN="$codex_stub" CODEX_PROXY_URL="http://127.0.0.1:$probe_port/" \
    AUTH_MODE=api-key MODEL=stub-model EFFORT=high PROMPT='stub prompt' \
    BOT_NAME=stub-bot BOT_ID=123 \
    GITHUB_TOKEN=runner-token-must-not-cross \
    OPENAI_API_KEY=runner-openai-key-must-not-cross \
    GITHUB_OUTPUT="$github_output" \
    GITHUB_STEP_SUMMARY="$runner_summary" \
    /usr/bin/python3 -E -s \
      "$TEND_TEST_ACTION_PATH/shared/steps/launch_sandbox_runtime.py" || rc=$?
  rm -rf "$private_action"
  kill "$probe_pid" 2>/dev/null || true
  wait "$probe_pid" 2>/dev/null || true
  test "$rc" -eq 0
  grep -qx 'sandbox_reaped=true' "$github_output"
  test "$(sed -n 's/^final_message=//p' "$github_output" | base64 -d)" = \
    'codex final'
  test "$(sudo -u "$SANDBOX" cat "$TEND_AGENT_WORKSPACE/.tend-codex-network")" = \
    'tend-srt-network-ok'
  test "$(sudo -u "$SANDBOX" cat "$TEND_AGENT_WORKSPACE/.tend-codex-local-network")" = \
    'tend-srt-local-ok'
  sudo -u "$SANDBOX" grep -qxF "HTTP_PROXY=$setup_proxy" "$codex_env"
  sudo -u "$SANDBOX" grep -qx 'NO_PROXY=' "$codex_env"
  sudo -u "$SANDBOX" grep -qx 'no_proxy=' "$codex_env"
  sudo -u "$SANDBOX" grep -q '^shell_environment_policy.set.NO_PROXY=".*127.0.0.1' \
    "$codex_argv"
  sudo -u "$SANDBOX" grep -q '^shell_environment_policy.set.no_proxy=".*127.0.0.1' \
    "$codex_argv"
  if sudo -u "$SANDBOX" grep -qE \
    'runner-token-must-not-cross|runner-openai-key-must-not-cross' "$codex_env"; then
    echo "::error::a runner credential crossed into Codex"
    exit 1
  fi
  echo "[test-setup-sandbox] complete Claude and Codex SRT lifecycles verified"
}

cleanup() {
  local shared
  shared="/opt/tend-sandbox-test-$GITHUB_RUN_ID/bin"
  if [ -n "${SANDBOX:-}" ]; then
    /usr/bin/sudo rm -rf -- "$TEND_AGENT_WORKSPACE"
    /usr/bin/sudo rm -rf -- "$TEND_TEST_ACTION_PATH"
  fi
  if [ -n "${TEND_AGENT_CONTAINER:-}" ]; then
    /usr/bin/sudo rmdir -- "$TEND_AGENT_CONTAINER" 2>/dev/null || true
  fi
  /usr/bin/sudo rm -f /usr/local/bin/tend-probe "$shared/tend-shared" "$shared/uv"
  /usr/bin/sudo rmdir "$shared" "${shared%/bin}" 2>/dev/null || true
  /usr/bin/sudo rm -f /etc/skel/.tend-seeded/bin/tend-seeded
  /usr/bin/sudo rmdir /etc/skel/.tend-seeded/bin \
    /etc/skel/.tend-seeded 2>/dev/null || true
}

case "${1:-}" in
  plant) plant ;;
  setup) setup ;;
  install-agent-uv) install_agent_uv ;;
  verify) verify ;;
  verify-refusals) verify_refusals ;;
  verify-srt) verify_srt ;;
  cleanup) cleanup ;;
  *)
    echo "usage: $0 {plant|setup|install-agent-uv|verify|verify-refusals|verify-srt|cleanup}" >&2
    exit 2
    ;;
esac
