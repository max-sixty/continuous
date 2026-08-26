#!/usr/bin/env bash
# Finish preparing the sandbox, immediately before the agent runs: execute the
# adopter's `sandbox_setup:` commands (from .config/tend.yaml, threaded in as
# TEND_SANDBOX_SETUP) INSIDE the sandbox, as the non-sudo sandbox user, then
# report which commands the runner can resolve and the agent cannot.
#
# `sandbox_setup:` is the general lever runner-side `setup:` cannot provide:
# `setup:` runs as the runner user around the composite action, while these
# commands run with the same launch env the agent gets
# ($AGENT_ENV_FILE: proxy routing, CA trust, dummy credentials, plus any
# sandbox_path/sandbox_env additions) and with the workspace as the working
# directory.
#
# Env-only tweaks (PATH, exported vars) do NOT persist to the agent from here —
# a child shell's exports die with it. Use `sandbox_path:` / `sandbox_env:` for
# those; use `sandbox_setup:` for actions with on-disk effects (installing a
# tool, warming a cache, generating a file).
#
# Inputs (env): TEND_SANDBOX_SETUP (the commands; empty → the report only),
# SANDBOX, AGENT_ENV_FILE, AGENT_PATH and TEND_BLOCKED_PATH (exported by
# setup-sandbox.sh via $GITHUB_ENV), plus the GITHUB_* context from Actions.
# Used by the Claude harness action.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/gha-context-env.sh
. "${SCRIPT_DIR}/lib/gha-context-env.sh"

if [ -n "${TEND_SANDBOX_SETUP:-}" ]; then
  # Run as the sandbox user with the agent's launch env. The commands go through
  # `bash -c`'s argument: no temp file (so no sandbox-side read permission on a
  # runner-owned path), and not stdin (so a setup command that reads stdin — an
  # installer prompt, `read` — can't swallow the remaining lines and exit 0).
  # `-e` inside so a failing setup command fails the step loudly rather than
  # silently proceeding to the run.
  # `sudo env` replaces the environment with only what is listed, so the
  # GITHUB_* context is re-passed from lib/gha-context-env.sh: one
  # `sandbox_setup:` block runs for every workflow and event, and a command
  # scopes itself with $GITHUB_WORKFLOW or $GITHUB_EVENT_NAME.
  mapfile -t AGENT_ENV <"$AGENT_ENV_FILE"
  gha_context_env
  sudo -u "$SANDBOX" env "${AGENT_ENV[@]}" "${GHA_CONTEXT_ENV[@]}" \
    bash -eo pipefail -c "$TEND_SANDBOX_SETUP"
  echo "[sandbox-setup] ran adopter sandbox_setup commands as $SANDBOX"
fi

# What the agent will not be able to run. Shared system/toolcache paths cross
# the UID boundary; runner-home paths do not. Home-selected commands have
# failure shims before shared paths so they cannot silently change version;
# sandbox_setup can shadow a shim from .local/bin. Reported, not fatal: only the
# adopter knows which tools its gate needs.
# A dir the lister can't read lists nothing (the glob stays literal), so its
# commands read as missing on that side. That's a false positive the diff can't
# distinguish from a real one; PATH directories are public on hosted runners.
list_commands='
IFS=:
for dir in $1; do
  [ -d "$dir" ] || continue
  for f in "$dir"/*; do
    if [ -f "$f" ] && [ -x "$f" ]; then printf "%s\n" "${f##*/}"; fi
  done
done'
# The sandbox side runs as the sandbox user, so the -x test answers the question
# that matters: can THAT uid execute it, not does the file exist.
runner_commands="$(bash -c "$list_commands" _ "$PATH" | sort -u || true)"
agent_commands="$(sudo -u "$SANDBOX" bash -c "$list_commands" _ "$AGENT_PATH" | sort -u || true)"
# Nothing on the agent's side means the listing failed, not that the agent has
# no commands — it always resolves /usr/bin. Reporting then would name every
# command the runner has. And `comm` exits 1 on input it judges unsorted, which
# under `set -e` would make this diagnostic the thing that kills the run.
# Neither failure may cost more than the report itself.
if [ -z "$agent_commands" ]; then
  echo "[sandbox-setup] could not list the agent's PATH; no reachability report"
else
  missing="$(comm -23 <(printf '%s\n' "$runner_commands") \
    <(printf '%s\n' "$agent_commands") || true)"
  # A blocker itself is executable, so the name diff above sees it. Add it back
  # only while no earlier sandbox_path/.local/bin command has replaced it.
  blocked=
  if [ -n "${TEND_BLOCKED_PATH:-}" ] && [ -d "$TEND_BLOCKED_PATH" ]; then
    blocked="$(sudo -u "$SANDBOX" /usr/bin/bash -c '
IFS=:
for shim in "$2"/*; do
  name=${shim##*/}
  for dir in $1; do
    if [ "$dir" = "$2" ]; then printf "%s\n" "$name"; break; fi
    if [ -f "$dir/$name" ] && [ -x "$dir/$name" ]; then break; fi
  done
done' _ "$AGENT_PATH" "$TEND_BLOCKED_PATH" | sort -u || true)"
  fi
  unavailable="$({ printf '%s\n' "$missing"; printf '%s\n' "$blocked"; } \
    | sed '/^$/d' | sort -u || true)"
  if [ -n "$unavailable" ]; then
    echo "[sandbox-setup] on the runner's PATH, unavailable to the agent:" \
      "$(tr '\n' ' ' <<<"$unavailable")"
    echo "[sandbox-setup] if the session needs one of those, install it as the" \
      "sandbox user with sandbox_setup: in .config/tend.yaml"
  fi
fi
