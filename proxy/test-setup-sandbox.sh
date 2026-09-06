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
}

setup() {
  local action_run agent_path hostile_python hostile_site path_entry
  set_inputs
  # The workspace path leads; a literal `~` exercises expansion against the
  # sandbox home. The configured directory may be populated later.
  # shellcheck disable=SC2088
  export TEND_SANDBOX_PATH="$GITHUB_WORKSPACE/.tend-explicit/bin"$'\n~/.tend-tilde/bin'
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
  action_run=${action_run//'${{ github.action_path }}'/"$GITHUB_WORKSPACE/claude"}
  /usr/bin/bash --noprofile --norc -eo pipefail -c "$action_run" \
    | tee "$RUNNER_TEMP/setup.log"
  test ! -e "$RUNNER_TEMP/uv-python-used"
  test ! -e "$RUNNER_TEMP/pythonpath-used"

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

install_agent_uv() {
  local action_run
  UV_VERSION=$(yq -e '.inputs.uv_version.default' claude/action.yaml)
  export UV_VERSION
  action_run=$(yq -er '.runs.steps[] | select(.name == "Install agent uv fallback (sandbox)") | .run' claude/action.yaml)
  action_run=${action_run//'${{ github.action_path }}'/"$GITHUB_WORKSPACE/claude"}
  /usr/bin/bash --noprofile --norc -eo pipefail -c "$action_run"
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
  sudo -u "$SANDBOX" test -x "$TEND_AGENT_UV_DIR/uv"
  grep -q "^PATH=.*:${TEND_AGENT_UV_DIR}$" "$AGENT_ENV_FILE"
  test "$(sudo -u "$SANDBOX" env "${agent_env[@]}" uv --version)" = adopter-uv

  # The dropped runner-home command is reported; shared, workspace, and
  # independently seeded sandbox-home commands are reachable and stay absent.
  python3 shared/steps/sandbox_setup.py | tee "$RUNNER_TEMP/report.log"
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

  # Both halves of the set shared/steps/_sandbox.py defines, asserted from
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
    python3 shared/steps/sandbox_setup.py | tee "$RUNNER_TEMP/report-after.log"
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

# Exercise codex/runner.py across the real uid boundary with a stub binary. The
# Python tests replace subprocess.run; this pins the actual sudo/env crossing
# and proves the runner's GitHub and OpenAI credentials do not reach Codex.
verify_codex_launch() {
  local stub launch_env argv_file github_output dummy_token
  stub="$GITHUB_WORKSPACE/.tend-explicit/bin/codex-stub"
  launch_env="$TEND_RUN_DIR/codex-launch-env.txt"
  argv_file="$TEND_RUN_DIR/codex-argv.txt"
  github_output="$RUNNER_TEMP/codex-github-output"
  dummy_token=$(sed -n 's/^GITHUB_TOKEN=//p' "$AGENT_ENV_FILE")
  test -n "$dummy_token"

  printf '%s\n' \
    '#!/usr/bin/env bash' \
    "env > '$launch_env'" \
    "printf '%s\\n' \"\$@\" > '$argv_file'" \
    'while [ "$#" -gt 0 ]; do' \
    '  if [ "$1" = --output-last-message ]; then' \
    '    printf "codex final\n" > "$2"' \
    '    break' \
    '  fi' \
    '  shift' \
    'done' \
    | sudo -u "$SANDBOX" tee "$stub" >/dev/null
  sudo -u "$SANDBOX" chmod +x "$stub"
  rm -f "$github_output"

  CODEX_BIN="$stub" AUTH_MODE=api-key \
    CODEX_PROXY_URL=http://127.0.0.1:1234 \
    CODEX_SANDBOX_MODE=danger-full-access MODEL=stub-model EFFORT=high \
    PROMPT='stub prompt' BOT_NAME=stub-bot BOT_ID=123 \
    GITHUB_TOKEN=runner-token-must-not-cross \
    OPENAI_API_KEY=runner-openai-key-must-not-cross \
    GITHUB_OUTPUT="$github_output" \
    python3 codex/runner.py run

  sudo cp "$launch_env" "$RUNNER_TEMP/codex-launch-env.txt"
  sudo cp "$argv_file" "$RUNNER_TEMP/codex-argv.txt"
  grep -q "^GITHUB_TOKEN=$dummy_token$" "$RUNNER_TEMP/codex-launch-env.txt"
  grep -q "^GITHUB_REPOSITORY=$GITHUB_REPOSITORY$" \
    "$RUNNER_TEMP/codex-launch-env.txt"
  grep -q '^BOT_NAME=stub-bot$' "$RUNNER_TEMP/codex-launch-env.txt"
  grep -q '^BOT_ID=123$' "$RUNNER_TEMP/codex-launch-env.txt"
  if grep -qE 'runner-token-must-not-cross|runner-openai-key-must-not-cross' \
    "$RUNNER_TEMP/codex-launch-env.txt"; then
    echo "::error::a runner credential crossed into Codex"
    exit 1
  fi
  if grep -qE '^GITHUB_(ENV|PATH|OUTPUT|STATE|STEP_SUMMARY)=' \
    "$RUNNER_TEMP/codex-launch-env.txt"; then
    echo "::error::a runner command-file path crossed into Codex"
    exit 1
  fi
  grep -qxF danger-full-access "$RUNNER_TEMP/codex-argv.txt"
  grep -qxF 'model_provider="tend-openai"' "$RUNNER_TEMP/codex-argv.txt"
  test "$(sed -n 's/^final_message=//p' "$github_output" | base64 -d)" = \
    'codex final'
  echo "[test-setup-sandbox] Codex credential crossing verified"
}

# The agent launch itself: shared/steps/run_claude.py composes the settings and
# the launch env, crosses the UID boundary, supervises, and reaches the verdict.
# Nothing else executes the crossing — the unit tests replace subprocess.run, and
# an adopter first runs it for real after a release. A stub `claude` on the
# sandbox PATH stands in for the agent, so this needs no Anthropic credential
# and costs seconds: what is under test is the crossing, the runner-owned
# redirects, the supervisor, and the reap, not the model.
verify_launch() {
  local stub launch_env argv_file rc want workflow
  stub="$GITHUB_WORKSPACE/.tend-explicit/bin/claude"
  launch_env="$TEND_RUN_DIR/launch-env.txt"
  argv_file="$TEND_RUN_DIR/argv.txt"

  # Records the environment and argv it was launched with, then emits the
  # stream-json a finished turn produces. Its stdout is the runner-owned
  # $STREAM_JSON.
  #
  # The paths and the behaviour are baked in rather than read from the
  # environment: `sudo`'s env_reset means the stub receives ONLY what
  # run_claude.py names on the crossing line, so a $TEND_STUB_* set here would
  # never reach it — the same property the assertions below are about.
  #
  # $1 seconds to sleep before finishing; $2 "ignore-term" to survive the
  # supervisor's SIGTERM, so the KILL escalation has to fire.
  write_stub() {
    printf '%s\n' \
      '#!/usr/bin/env bash' \
      "env > '$launch_env'" \
      "printf '%s\\n' \"\$@\" > '$argv_file'" \
      "${2:+trap '' TERM}" \
      "${1:+sleep $1}" \
      'printf "%s\\n" "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"stub turn\"}]}}"' \
      'printf "%s\\n" "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false}"' \
      | sudo -u "$SANDBOX" tee "$stub" >/dev/null
    sudo -u "$SANDBOX" chmod +x "$stub"
  }

  export TEND_MODEL=stub-model TEND_ALLOWED_TOOLS='Bash,Read' \
    TEND_SYSTEM_PROMPT='stub system prompt' TEND_PROMPT='stub prompt' \
    SHOW_FULL_OUTPUT=true BOT_NAME=stub-bot BOT_ID=1 \
    CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0

  # --- a turn that finishes ---
  write_stub "" ""
  TEND_TIMEOUT_SEC=60 GITHUB_TOKEN=runner-token-must-not-cross \
    python3 shared/steps/run_claude.py
  test -s "$RUNNER_TEMP/tend-stream.json" \
    || { echo "::error::the sandbox's stdout did not reach the runner-owned stream-json"; exit 1; }
  grep -q '"stub turn"' "$RUNNER_TEMP/tend-stream.json"
  sudo test -s "$launch_env" \
    || { echo "::error::the stub recorded no launch env"; exit 1; }
  sudo cp "$launch_env" "$RUNNER_TEMP/launch-env.txt"
  sudo cp "$argv_file" "$RUNNER_TEMP/argv.txt"

  # Everything that steers the agent is argv, so the env assertions below say
  # nothing about it. One line per element, exactly as the stub recorded them.
  for want in -p --model stub-model --permission-mode bypassPermissions \
    --allowedTools 'Bash,Read' --append-system-prompt 'stub system prompt' \
    --output-format stream-json --verbose 'stub prompt'; do
    grep -qxF -- "$want" "$RUNNER_TEMP/argv.txt" \
      || { echo "::error::the agent was not launched with '$want'"; exit 1; }
  done

  # The GitHub context crossed, tend's own assignments crossed, and the
  # runner's real token did not — the file's dummy is what the agent sees.
  grep -q "^GITHUB_REPOSITORY=$GITHUB_REPOSITORY$" "$RUNNER_TEMP/launch-env.txt"
  grep -q '^BOT_NAME=stub-bot$' "$RUNNER_TEMP/launch-env.txt"
  if grep -q '^GITHUB_TOKEN=runner-token-must-not-cross$' "$RUNNER_TEMP/launch-env.txt"; then
    echo "::error::the runner's GITHUB_TOKEN crossed into the sandbox"
    exit 1
  fi
  if grep -q '^GITHUB_ENV=' "$RUNNER_TEMP/launch-env.txt"; then
    echo "::error::GITHUB_ENV crossed, handing the sandbox a channel into later steps"
    exit 1
  fi
  # `sandbox_env` set this name in $AGENT_ENV_FILE; the real context must win.
  workflow=$(sed -n 's/^GITHUB_WORKFLOW=//p' "$RUNNER_TEMP/launch-env.txt")
  test "$workflow" != spoofed-by-sandbox-env \
    || { echo "::error::sandbox_env displaced the real GITHUB_WORKFLOW"; exit 1; }

  # Composed for the agent, written as the sandbox user into its workspace.
  sudo -u "$SANDBOX" test -r "$GITHUB_WORKSPACE/.claude/settings.local.json"
  sudo -u "$SANDBOX" grep -q bypassPermissions \
    "$GITHUB_WORKSPACE/.claude/settings.local.json"

  # --- a turn that overruns and refuses to die ---
  # The stub traps TERM and sleeps past the bound, so the supervisor's TERM
  # lands and is ignored, the grace period expires, and only the KILL ends it —
  # the whole escalation, against a real uid boundary. A cooperative turn would
  # exercise none of it: it would end before the bound, and sudo's own teardown
  # would collect its tree.
  write_stub 300 ignore-term
  rc=0
  TEND_TIMEOUT_SEC=2 python3 shared/steps/run_claude.py \
    >"$RUNNER_TEMP/timeout.log" 2>&1 || rc=$?
  test "$rc" -eq 1 \
    || { echo "::error::an overrunning turn exited $rc, not 1"; cat "$RUNNER_TEMP/timeout.log"; exit 1; }
  grep -q 'status=timeout' "$RUNNER_TEMP/timeout.log" \
    || { echo "::error::the supervisor did not classify the overrun as a timeout"; cat "$RUNNER_TEMP/timeout.log"; exit 1; }
  grep -q 'exceeded 2s timeout' "$RUNNER_TEMP/timeout.log"
  if grep -q 'exited non-zero' "$RUNNER_TEMP/timeout.log"; then
    echo "::error::the bound overrun was reported as a crash"
    exit 1
  fi
  if sudo pgrep -u "$SANDBOX" >/dev/null 2>&1; then
    echo "::error::a sandbox-owned process survived the supervisor's teardown"
    sudo pgrep -alu "$SANDBOX" || true
    exit 1
  fi

  # --- a turn that overruns and takes the TERM ---
  # The same overrun without the trap, so the stub dies inside the grace and
  # the supervisor's `wait(TERM_GRACE_SEC)` returns rather than raising. That
  # is the branch every real timeout takes — an agent that ignores TERM is the
  # exception — and it is otherwise reached only through a fake.
  write_stub 300 ""
  rc=0
  TEND_TIMEOUT_SEC=2 python3 shared/steps/run_claude.py \
    >"$RUNNER_TEMP/term.log" 2>&1 || rc=$?
  test "$rc" -eq 1 \
    || { echo "::error::a cooperative overrun exited $rc, not 1"; cat "$RUNNER_TEMP/term.log"; exit 1; }
  grep -q 'status=timeout' "$RUNNER_TEMP/term.log" \
    || { echo "::error::a cooperative overrun was not classified as a timeout"; cat "$RUNNER_TEMP/term.log"; exit 1; }
  grep -q 'exceeded 2s timeout' "$RUNNER_TEMP/term.log"
  if sudo pgrep -u "$SANDBOX" >/dev/null 2>&1; then
    echo "::error::a sandbox-owned process survived the cooperative teardown"
    sudo pgrep -alu "$SANDBOX" || true
    exit 1
  fi
  echo "[test-setup-sandbox] launch, argv, supervision and teardown verified"
}

cleanup() {
  local shared
  shared="/opt/tend-sandbox-test-$GITHUB_RUN_ID/bin"
  if [ -n "${SANDBOX:-}" ]; then
    /usr/bin/sudo chown -R "$(id -u):$(id -g)" "$GITHUB_WORKSPACE"
  fi
  /usr/bin/sudo rm -f "$GITHUB_WORKSPACE/.claude/settings.local.json"
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
  verify-codex-launch) verify_codex_launch ;;
  verify-launch) verify_launch ;;
  cleanup) cleanup ;;
  *)
    echo "usage: $0 {plant|setup|install-agent-uv|verify|verify-refusals|verify-codex-launch|verify-launch|cleanup}" >&2
    exit 2
    ;;
esac
