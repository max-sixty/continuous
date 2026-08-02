#!/usr/bin/env bash
# Shared preflight: refuse to run unless the default branch is protected
# against the bot itself. Shared verbatim by both harness actions
# (claude/, codex/).
#
# Runs with the bot's own token, so `current_user_can_bypass` on each
# applying ruleset is GitHub's answer to "can this bot bypass?" — teams,
# custom roles, and org/enterprise-sourced rulesets all evaluated
# server-side. One update rule the bot cannot bypass proves the bot cannot
# update the branch. If update rules exist but the bot can bypass every
# one, the merge restriction provably does not restrict the bot and the
# run aborts. Repos protected by required reviews alone have no update
# rule and fall back to the `.protected` floor, as before.
#
# Inputs (env): GITHUB_TOKEN (the bot's token, for gh), GITHUB_REPOSITORY
# (from Actions).
set -eo pipefail

DEFAULT_BRANCH=$(gh api "repos/${GITHUB_REPOSITORY}" --jq '.default_branch')

RULESET_IDS=$(gh api "repos/${GITHUB_REPOSITORY}/rules/branches/${DEFAULT_BRANCH}" \
  --jq '[.[] | select(.type == "update") | .ruleset_id] | unique | .[]' || true)

VERDICT="no-update-rules" # or: blocked | bypassable
while IFS= read -r id; do
  [ -n "$id" ] || continue
  CAN_BYPASS=$(gh api "repos/${GITHUB_REPOSITORY}/rulesets/${id}" \
    --jq '.current_user_can_bypass' || echo "unreadable")
  if [ "$CAN_BYPASS" = "never" ]; then
    VERDICT="blocked"
    break
  elif [ "$CAN_BYPASS" != "unreadable" ]; then
    VERDICT="bypassable"
  fi
done <<<"$RULESET_IDS"

if [ "$VERDICT" = "blocked" ]; then
  echo "Security preflight passed: bot cannot bypass the restrict-updates ruleset on '${DEFAULT_BRANCH}'"
  exit 0
fi
if [ "$VERDICT" = "bypassable" ]; then
  echo "::error::The bot can bypass every restrict-updates ruleset on '${DEFAULT_BRANCH}' (current_user_can_bypass != never), so the merge restriction does not restrict the bot. Remove the bot — or any team, role, or user exemption covering it — from the rulesets' bypass actors. See docs/security-model.md in the Tend repo."
  exit 1
fi

# No update rules apply (or none were readable): fall back to requiring
# that the branch is protected at all, e.g. by required reviews.
PROTECTED=$(gh api "repos/${GITHUB_REPOSITORY}/branches/${DEFAULT_BRANCH}" --jq '.protected')
if [ "$PROTECTED" != "true" ]; then
  echo "::error::Default branch '${DEFAULT_BRANCH}' is NOT protected. Without branch protection, the bot can merge PRs without review. Add a branch protection rule or ruleset before using Tend. See docs/security-model.md in the Tend repo."
  exit 1
fi
echo "Security preflight passed: default branch '${DEFAULT_BRANCH}' is protected"
