#!/usr/bin/env bash
# The environment the sandbox user is launched with. Sourced, not executed.
#
# Every step that hands the sandbox an environment does so through
# `sudo -u "$SANDBOX" env`: `env_reset` drops the runner's, so only the names on
# that line reach the child. The two crossings that put the ADOPTER's code on
# the far side build that line here — the agent launch (claude/action.yaml) and
# their `sandbox_setup:` commands (sandbox-setup.sh) — so a `sandbox_setup:`
# command can gate an install on the variable the skill it installs for will
# read. The crossings that run tend's own code take no GitHub context. Which
# side of that line a new crossing falls on is who wrote what runs there, not
# how much context looks useful.
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

# sandbox_launch_env <agent-env-file> — set SANDBOX_LAUNCH_ENV to the
# NAME=VALUE arguments for `sudo -u "$SANDBOX" env`: the file's lines (proxy
# routing, CA trust, dummy credentials, and the adopter's own `sandbox_env:`
# additions), then the GitHub context.
#
# That order is the reason this composes both halves rather than handing back
# the context alone. `env` takes the final assignment of a name, and the file is
# the half an adopter writes, so the context has to follow it or a
# `sandbox_env: {GITHUB_WORKFLOW: …}` would decide what the run thinks it is. As
# two arrays that was a rule each caller had to remember; here it is the
# function's postcondition. A caller may append names of its own afterwards —
# they win, which is what tend's own BOT_*/TEND_* assignments want, since those
# have to beat the file — provided none is GITHUB_*-named or a key the file
# defines, which would put the context or the sandbox's routing back in play.
#
# Reads the environment when called, so call it in the step that forwards it.
sandbox_launch_env() {
  local name
  mapfile -t SANDBOX_LAUNCH_ENV <"$1"
  while IFS= read -r name; do
    case "$name" in
      GITHUB_TOKEN | GITHUB_ENV | GITHUB_PATH | GITHUB_OUTPUT | GITHUB_STATE | GITHUB_STEP_SUMMARY)
        continue
        ;;
    esac
    SANDBOX_LAUNCH_ENV+=("$name=${!name}")
  done < <(compgen -e | grep '^GITHUB_')
}
