# shellcheck shell=bash
# Establish the repository's frequent maintenance queue and decide whether the
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

# GitHub computes mergeability lazily after the base moves. UNKNOWN therefore
# means "worth a synchronous local test", not "clean". This is only a cheap
# boot gate; the agent test-merges every candidate before changing a branch.
# Read the newest comments: a deferral is normally the PR's latest activity.
# An older marker can waste boots, but the resolver paginates before acting.
if BOT_LOGIN=$(gh api user --jq .login 2>/dev/null) \
  && PRS=$(gh api graphql -f query='
    query($q: String!) {
      search(query: $q, type: ISSUE, first: 100) {
        nodes { ... on PullRequest {
          mergeable headRefOid
          comments(last: 100) { nodes { author { login } body } }
        } }
      }
    }' -f q="repo:$GITHUB_REPOSITORY author:$BOT_LOGIN is:pr is:open" \
    --jq '.data.search.nodes' 2>/dev/null) \
  && CONFLICT_COUNT=$(jq -er --arg bot "$BOT_LOGIN" '
    [.[]
      | select(.mergeable != "MERGEABLE")
      | . as $pr
      | "<!-- tend-conflict-deferred head=\($pr.headRefOid) -->" as $marker
      | select(any($pr.comments[]?;
          .author.login == $bot
          and (((.body // "") | sub("\\s+$"; "") | split("\n") | last) == $marker))
        | not)]
    | length' <<<"$PRS"); then
  :
else
  CONFLICT_COUNT=0
  echo "::warning::bot PR conflict scan failed; retrying next cycle"
fi

echo "conflict_count=$CONFLICT_COUNT" >> "$GITHUB_OUTPUT"

if [ "$COUNT" = "0" ] && [ "$CONFLICT_COUNT" = "0" ]; then
  echo "No notification or conflict work — skipping"
else
  [ "$COUNT" = "0" ] || \
    echo "$COUNT notification task(s) — proceeding"
  [ "$CONFLICT_COUNT" = "0" ] || \
    echo "$CONFLICT_COUNT possible conflicted bot PR(s) — proceeding"
fi
