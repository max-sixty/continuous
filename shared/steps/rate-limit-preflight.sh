#!/usr/bin/env bash
# Shared preflight: abort if the bot is creating issues/PRs faster than a
# burst limit (20-minute window) or a daily spike limit (vs a 6-day baseline).
# Shared verbatim by both harness actions.
#
# Inputs (env): GITHUB_TOKEN (for gh), BOT_NAME (bot username),
# GITHUB_REPOSITORY (from Actions).
set -eo pipefail

REPO="${GITHUB_REPOSITORY}"
BOT="${BOT_NAME}"
# GNU date — runs on Ubuntu (GitHub Actions runners)
TWENTY_MIN_AGO=$(date -u -d '20 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
TODAY=$(date -u +%Y-%m-%d)
YESTERDAY=$(date -u -d 'yesterday' +%Y-%m-%d)
SIX_DAYS_AGO=$(date -u -d '6 days ago' +%Y-%m-%d)

# Burst check: too many in the last 20 minutes
RECENT_PRS=$(gh api "repos/$REPO/pulls?state=all&sort=created&direction=desc&per_page=30" \
  --jq "[.[] | select(.user.login == \"$BOT\" and .created_at > \"$TWENTY_MIN_AGO\")] | length" || echo 0)
RECENT_ISSUES=$(gh api "repos/$REPO/issues?creator=$BOT&state=all&sort=created&direction=desc&per_page=30" \
  --jq "[.[] | select(.pull_request == null and .created_at > \"$TWENTY_MIN_AGO\")] | length" || echo 0)

# Spike check: abnormal daily volume vs 6-day baseline
# search/issues covers both issues and PRs
TODAY_POSTS=$(gh api "search/issues?q=author:${BOT}+repo:${REPO}+created:${TODAY}" \
  --jq '.total_count' || echo 0)
PAST_POSTS=$(gh api "search/issues?q=author:${BOT}+repo:${REPO}+created:${SIX_DAYS_AGO}..${YESTERDAY}" \
  --jq '.total_count' || echo 0)
# spike_limit = 10 + 2 * daily_avg = 10 + 2 * (past_posts / 6) = 10 + past_posts / 3
SPIKE_LIMIT=$((10 + PAST_POSTS / 3))

echo "Rate limit: burst=${RECENT_PRS} PRs, ${RECENT_ISSUES} issues (20min); today=${TODAY_POSTS} (limit: ${SPIKE_LIMIT})"

ABORT=false
if [ "$RECENT_PRS" -gt 10 ]; then
  echo "::error::Rate limit: bot created ${RECENT_PRS} PRs in the last 20 minutes (limit: 10)"
  ABORT=true
fi
if [ "$RECENT_ISSUES" -gt 10 ]; then
  echo "::error::Rate limit: bot created ${RECENT_ISSUES} issues in the last 20 minutes (limit: 10)"
  ABORT=true
fi
if [ "$TODAY_POSTS" -gt "$SPIKE_LIMIT" ]; then
  echo "::error::Rate limit: bot created ${TODAY_POSTS} items today, above spike limit of ${SPIKE_LIMIT} (baseline: ${PAST_POSTS} over past 6 days)"
  ABORT=true
fi
if [ "$ABORT" = true ]; then
  exit 1
fi
echo "Rate limit check passed"
