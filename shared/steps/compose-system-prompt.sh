#!/usr/bin/env bash
# Compose the system prompt from shared/system-prompt.md (harness-neutral bulk:
# conduct, scope, precedence) plus a Claude-specific skill-loading directive and
# a CI-autonomy directive, then append the adopter's `system_prompt_append`
# input if non-empty. Used by the Claude harness action. Codex consumes
# the same shared/system-prompt.md from codex/action.yaml — keep edits to that
# file in sync across both engines.
#
# Emits the composed prompt to $GITHUB_OUTPUT under `value`, which the caller
# exposes as steps.<id>.outputs.value.
#
# Inputs (env): SYSTEM_PROMPT_FILE (absolute path to shared/system-prompt.md;
# differs per action by checkout depth), BOT_NAME, EXTRA (system_prompt_append),
# GITHUB_OUTPUT (from Actions).
set -eo pipefail

SHARED="$SYSTEM_PROMPT_FILE"
CLAUDE_DIRECTIVE="Invoke the skill the prompt names — it drives the whole run, and a run that skips it exits clean having done nothing. Use /tend-ci-runner:running-in-ci before starting work."
AUTONOMY_DIRECTIVE="You are running in CI; no human is available to answer questions. Never prompt for clarification or approval. When uncertain, make the best reasonable choice from the available evidence and proceed. Permissions are pre-approved; tool calls execute without confirmation."
BASE=$(BOT_NAME="$BOT_NAME" envsubst '$BOT_NAME' < "$SHARED")
FULL="${CLAUDE_DIRECTIVE}"$'\n\n'"${AUTONOMY_DIRECTIVE}"$'\n\n'"${BASE}"
if [ -n "$EXTRA" ]; then
  FULL="${FULL}"$'\n\n'"${EXTRA}"
fi
{
  echo 'value<<TEND_EOF'
  printf '%s\n' "$FULL"
  echo 'TEND_EOF'
} >> "$GITHUB_OUTPUT"
