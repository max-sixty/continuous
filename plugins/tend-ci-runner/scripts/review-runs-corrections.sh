#!/usr/bin/env bash
# Collects every in-window signal that a maintainer corrected the bot, as one
# JSON object.
#
# Usage: review-runs-corrections.sh <since>
#   <since>  RFC3339 window anchor — review-runs Step 1's, via
#            /tmp/review-runs-since
#
# Output (one object, all keys always present):
#   since         the anchor, echoed back
#   bot           the login every list below filters out
#   dispositions  bot PRs merged or closed in the window
#   comments      non-bot comments on any issue or PR thread in the window
#   reviews       non-bot review bodies submitted in the window
#
# Both filters fail open when empty — `--author ""` applies no author filter,
# `.user.login != ""` is true for every entry, and an empty anchor compares
# less than every non-null timestamp — so a missing one turns the whole query
# into the bot reading its own output as maintainer corrections. Neither is
# defaulted: an empty result here becomes "no maintainer corrections" in the
# tracking issue, which later runs read as ground truth.
#
# env: GITHUB_REPOSITORY (optional; falls back to the checkout's remote)

set -euo pipefail

SINCE="${1:-}"
if [ -z "$SINCE" ]; then
  echo "usage: $(basename "$0") <since>   # RFC3339, e.g. \$(cat /tmp/review-runs-since)" >&2
  exit 2
fi

REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
BOT=$(gh api user --jq '.login')
if [ -z "$BOT" ]; then
  echo "could not resolve the bot login from \`gh api user\`" >&2
  exit 2
fi

DISPOSITIONS=$(gh pr list --repo "$REPO" --author "$BOT" --state all --limit 200 \
  --json number,title,state,closedAt \
  | jq --arg since "$SINCE" '[.[] | select(.closedAt > $since)]')

# Both comment endpoints are repo-wide and take `since`, so this is two calls
# regardless of thread count. `--paginate` is required, not cosmetic: the
# response is ascending by `created_at`, so a single 100-item page drops the
# *newest* comments — the ones a fresh correction sits in — and the bot's own
# comments consume that budget first. `since` filters on `updated_at`, so an
# older comment edited inside the window is a real hit, not a broken filter;
# both timestamps are reported so the reason a row qualified is visible.
COMMENTS=$(
  for endpoint in issues pulls; do
    gh api --paginate "repos/$REPO/$endpoint/comments?since=$SINCE&per_page=100"
  done | jq -s --arg bot "$BOT" '
    [(add // [])[] | select(.user.login != $bot)
     | {created: .created_at, updated: .updated_at, url: .html_url,
        body: .body[0:300]}]'
)

# Review bodies, which neither endpoint above returns — `pulls/comments`
# carries inline comments only. There is no repo-wide `since` endpoint for
# reviews, so loop the PRs updated in the window. `updated:` windows the PR
# rather than the review, so the `submitted_at` filter is what makes the review
# window exact; `(.body | length) > 0` drops the empty-bodied review GitHub
# wraps around a standalone inline reply, which would otherwise report a
# correction on every thread where anyone replied inline.
#
# The candidate list is its own assignment because `set -e` checks an
# assignment's command substitution but not a `for` word list's: inline, a
# failed `--search` — the search API's 30 req/min, not the 5000/h core budget —
# would yield an empty list, skip the loop, and report `reviews: []` as an
# all-clear.
CANDIDATES=$(gh pr list --repo "$REPO" --state all --limit 200 \
  --search "updated:>=$SINCE" --json number --jq '.[].number')
REVIEWS=$(
  for pr in $CANDIDATES; do
    gh api --paginate "repos/$REPO/pulls/$pr/reviews?per_page=100"
  done | jq -s --arg bot "$BOT" --arg since "$SINCE" '
    [(add // [])[] | select(.user.login != $bot and .submitted_at >= $since
                            and (.body | length) > 0)
     | {at: .submitted_at, state: .state, url: .html_url, body: .body[0:300]}]'
)

jq -n --arg since "$SINCE" --arg bot "$BOT" \
  --argjson dispositions "$DISPOSITIONS" \
  --argjson comments "$COMMENTS" \
  --argjson reviews "$REVIEWS" \
  '{since: $since, bot: $bot, dispositions: $dispositions,
    comments: $comments, reviews: $reviews}'
