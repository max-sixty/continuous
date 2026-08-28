# shellcheck shell=bash
# Establish the repository's durable notification queue and decide whether the
# agent needs to boot. Inlined into the generated workflow: env in,
# GITHUB_OUTPUT out.
#
# env: GITHUB_REPOSITORY, GITHUB_OUTPUT, GITHUB_TOKEN

# Activity newer than this belongs to an event workflow that may still be
# running. The same cutoff is passed to the agent and, once every older item has
# a semantic outcome, to GitHub's repository-level mark-read endpoint. Newer
# activity therefore cannot be acknowledged by this run.
CUTOFF=$(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
echo "cutoff=$CUTOFF" >> "$GITHUB_OUTPUT"

# Watching makes a new issue or PR visible before the bot has participated in
# its thread. The installer sets this too; every poll repeats the idempotent PUT
# so a later settings change is repaired without additional state.
gh api "repos/$GITHUB_REPOSITORY/subscription" -X PUT \
  -F subscribed=true -F ignored=false --silent \
  || echo "::warning::could not enable repository watching; retrying next cycle"

# Capture every unread page at the cutoff. GitHub occasionally returns an HTML
# error page even with a successful status, so validate the slurped page shape.
# A failed fetch leaves the queue untouched for the next scheduled cycle.
ENDPOINT="notifications?before=$CUTOFF&per_page=100"
if PAGES=$(gh api "$ENDPOINT" --paginate --slurp 2>/dev/null) \
  && NOTIFS=$(echo "$PAGES" | jq -ce \
    'if type == "array" and all(.[]; type == "array") then add // [] else error("invalid pages") end'); then
  COUNT=$(echo "$NOTIFS" | jq 'length')
else
  COUNT=0
  echo "::warning::notifications fetch failed; queue left for the next cycle"
fi

echo "count=$COUNT" >> "$GITHUB_OUTPUT"

if [ "$COUNT" = "0" ]; then
  echo "No notification work before $CUTOFF — skipping"
else
  echo "$COUNT notification task(s) — proceeding"
fi
