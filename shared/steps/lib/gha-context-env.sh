#!/usr/bin/env bash
# The GitHub context handed to the sandbox user. Sourced, not executed.
#
# Every step that hands the sandbox an environment does so through
# `sudo -u "$SANDBOX" env`: `env_reset` drops the runner's, so only the names on
# that line reach the child. The two crossings that put the ADOPTER's code on
# the far side share this definition — the agent launch (claude/action.yaml) and
# their `sandbox_setup:` commands (sandbox-setup.sh) — so a `sandbox_setup:`
# command can gate an install on the variable the skill it installs for will
# read. The crossings that run tend's own code take none. Which side of that
# line a new crossing falls on is who wrote what runs there, not how much
# context looks useful.
#
# Pass GITHUB_* through as a denylist rather than an explicit allowlist: most
# GITHUB_* vars are informational (GITHUB_ACTOR, GITHUB_API_URL,
# GITHUB_REF_NAME, GITHUB_WORKSPACE, …) and a denylist picks up future
# additions automatically. Skills depend on them for run-self-reference (branch
# names, gist headings, dedup of own check runs) and owner-correct URL
# construction. Apart from the exclusions below, every GITHUB_* Actions defines
# is public rather than a secret; a GITHUB_*-named variable an adopter's
# `setup:` step writes to $GITHUB_ENV crosses on the same rule, so a secret must
# not be given a GITHUB_* name. Exclude:
#   GITHUB_TOKEN — the agent env file carries a dummy; the real PAT lives in
#     the proxy. (The file's dummy must not be overridden, so the denylist
#     entry is load-bearing.)
#   GITHUB_{ENV,PATH,OUTPUT,STATE,STEP_SUMMARY} — paths the runner re-reads
#     after the step exits; the sandbox must not be handed a channel into
#     later steps' env / PATH / outputs / job summary.

# gha_context_env — set GHA_CONTEXT_ENV to the NAME=VALUE arguments for the
# `sudo … env` that crosses the boundary. It reads the environment at call
# time, so call it in the step that forwards it, and place the array LAST on
# that command line: `env` takes the final assignment of a name, so a trailing
# position keeps an adopter's `sandbox_env:` from displacing the real context
# with a value of its own.
gha_context_env() {
  local name
  GHA_CONTEXT_ENV=()
  while IFS= read -r name; do
    case "$name" in
      GITHUB_TOKEN | GITHUB_ENV | GITHUB_PATH | GITHUB_OUTPUT | GITHUB_STATE | GITHUB_STEP_SUMMARY)
        continue
        ;;
    esac
    GHA_CONTEXT_ENV+=("$name=${!name}")
  done < <(compgen -e | grep '^GITHUB_')
}
