#!/usr/bin/env bash
# Finish preparing the sandbox, immediately before the agent runs: execute the
# adopter's `sandbox_setup:` commands (from .config/tend.yaml, threaded in as
# TEND_SANDBOX_SETUP) INSIDE the sandbox, as the non-sudo sandbox user, then
# report which commands the runner can resolve and the agent cannot.
#
# `sandbox_setup:` is the general lever runner-side `setup:` can't provide:
# `setup:` runs as the runner user around the composite action and never reaches
# the sandbox env. Commands run with the same launch env the agent gets
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
# SANDBOX, AGENT_ENV_FILE and AGENT_PATH (exported by setup-sandbox.sh via
# $GITHUB_ENV).
# Used by the Claude harness action.
set -euo pipefail

if [ -n "${TEND_SANDBOX_SETUP:-}" ]; then
  # Run as the sandbox user with the agent's launch env. The commands go through
  # `bash -c`'s argument: no temp file (so no sandbox-side read permission on a
  # runner-owned path), and not stdin (so a setup command that reads stdin — an
  # installer prompt, `read` — can't swallow the remaining lines and exit 0).
  # `-e` inside so a failing setup command fails the step loudly rather than
  # silently proceeding to the run.
  mapfile -t AGENT_ENV <"$AGENT_ENV_FILE"
  sudo -u "$SANDBOX" env "${AGENT_ENV[@]}" bash -eo pipefail -c "$TEND_SANDBOX_SETUP"
  echo "[sandbox-setup] ran adopter sandbox_setup commands as $SANDBOX"
fi

# What the agent won't be able to run. The sandbox PATH is the runner's with
# every runner-home entry rewritten to the sandbox's own copy and dropped when
# that copy doesn't exist (setup-sandbox.sh), so a tool a `setup:` step
# installed under /home/runner is simply absent — no error, nothing in the log,
# until the agent hits `command not found` mid-session and works around it
# (skipping the check, or reaching for a weaker substitute). Diffing the two
# PATHs by resolvable command name names the gap here, after sandbox_setup has
# had its chance to close it. Reported, not fatal: most of what a runner carries
# is irrelevant to a given session, and only the adopter knows which tools their
# gate needs — asserting that belongs in their own `sandbox_setup:`.
# `-r` as well as `-d`: listing a dir needs read, executing from it needs only
# traverse, so a 0711 PATH dir would list empty and report everything in it as
# missing. Skipping it under-reports instead, which is the right way for a
# diagnostic to be wrong.
list_commands='
IFS=:
for dir in $1; do
  { [ -d "$dir" ] && [ -r "$dir" ]; } || continue
  for f in "$dir"/*; do
    if [ -f "$f" ] && [ -x "$f" ]; then printf "%s\n" "${f##*/}"; fi
  done
done'
# The sandbox side runs as the sandbox user, so the -x test answers the question
# that matters: can THAT uid execute it, not does the file exist.
runner_commands="$(bash -c "$list_commands" _ "$PATH" | sort -u)"
agent_commands="$(sudo -u "$SANDBOX" bash -c "$list_commands" _ "$AGENT_PATH" | sort -u)"
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
  if [ -n "$missing" ]; then
    echo "[sandbox-setup] on the runner's PATH, not the agent's:" \
      "$(tr '\n' ' ' <<<"$missing")"
    echo "[sandbox-setup] if the session needs one of those, install it as the" \
      "sandbox user with sandbox_setup: in .config/tend.yaml"
  fi
fi
