#!/usr/bin/env bash
# Shared preflight against runaway issue/PR creation. Shared verbatim by both
# harness actions. Three tiers, because the two failure shapes it guards against
# want different responses:
#
#   burst (>10 PRs or >10 issues in 20 minutes)  -> abort: only a loop does this
#   today > hard limit (a 6-day baseline's worth
#     of output in one day)                      -> abort: slow runaway
#   today > spike limit                          -> creation pause, run continues
#
# The spike tier used to abort too. It is a *creation* limit, so aborting took
# down work that creates nothing — reviews, mention replies, triage comments,
# CI fixes — for the rest of the UTC day, and a busy day after a quiet week
# (which depresses the baseline) trips it. Instead the run proceeds with
# TEND_CREATION_PAUSED_NOTE in the environment; both actions append it to the
# agent's prompt, so the agent still does its comment/review/push work and
# leaves new issues and PRs alone. The two abort tiers keep a hard stop that no
# prompt-injected session can talk its way past.
#
# Inputs (env): GITHUB_TOKEN (for gh), BOT_NAME (bot username),
# GITHUB_REPOSITORY (from Actions).
# Outputs (env, via GITHUB_ENV): TEND_CREATION_PAUSED_NOTE, set only on a
# spike-tier trip.
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
# hard_limit = 10 + past_posts: a whole baseline week's output in a single day.
# No productive day reaches it; a loop does.
#
# Both limits scale off PAST_POSTS, so they converge as the baseline falls: at
# a baseline of 0 they are both 10 and the pause tier is unreachable — the run
# hard-aborts on the 11th item, exactly the behaviour this script moved away
# from. A depressed baseline is the failure mode being fixed (a quota outage, a
# quiet week, an adopter's install day), so the pause tier has to survive it.
# Floor the gap at 10, matching the burst limit and the constant term in both
# formulas. It only binds below a baseline of ~15; above that the formula
# already opens a wider band.
HARD_LIMIT=$((10 + PAST_POSTS))
if [ "$HARD_LIMIT" -lt $((SPIKE_LIMIT + 10)) ]; then
  HARD_LIMIT=$((SPIKE_LIMIT + 10))
fi

echo "Rate limit: burst=${RECENT_PRS} PRs, ${RECENT_ISSUES} issues (20min); today=${TODAY_POSTS} (spike: ${SPIKE_LIMIT}, hard: ${HARD_LIMIT})"

ABORT=false
if [ "$RECENT_PRS" -gt 10 ]; then
  echo "::error::Rate limit: bot created ${RECENT_PRS} PRs in the last 20 minutes (limit: 10)"
  ABORT=true
fi
if [ "$RECENT_ISSUES" -gt 10 ]; then
  echo "::error::Rate limit: bot created ${RECENT_ISSUES} issues in the last 20 minutes (limit: 10)"
  ABORT=true
fi
if [ "$TODAY_POSTS" -gt "$HARD_LIMIT" ]; then
  echo "::error::Rate limit: bot created ${TODAY_POSTS} items today, above hard limit of ${HARD_LIMIT} (baseline: ${PAST_POSTS} over past 6 days)"
  ABORT=true
elif [ "$TODAY_POSTS" -gt "$SPIKE_LIMIT" ]; then
  echo "::warning::Creation pause: bot created ${TODAY_POSTS} items today, above spike limit of ${SPIKE_LIMIT} (baseline: ${PAST_POSTS} over past 6 days). The run continues; opening new issues and PRs is off-limits for it."
  # Skip the note when a burst check already set ABORT: the run exits 1 below,
  # so exporting a creation directive for an agent that never starts is dead
  # state that contradicts the invariant the abort tiers otherwise hold.
  if [ "$ABORT" = false ] && [ -n "${GITHUB_ENV:-}" ]; then
    {
      echo 'TEND_CREATION_PAUSED_NOTE<<TEND_EOF'
      echo "Creation pause: you have already opened ${TODAY_POSTS} issues and pull requests on this repository today, above the daily limit of ${SPIKE_LIMIT} that guards against runaway creation. For this run, do not open a new issue or pull request. Nothing else is restricted — reviews, review replies, issue and PR comments, and pushes to branches that already exist all proceed as normal. Do the work either way; only the filing is paused. Record the result where it will be read rather than dropping it: if this run has a triggering issue or PR, describe what you would have filed in a comment on that thread. If it does not — a scheduled or workflow_run run has no thread — write the finding to /tmp/claude/step-summary.md, which a later step copies into this run's job summary, and leave the filing to a maintainer or to the next run after the daily count resets at UTC midnight."
      echo 'TEND_EOF'
    } >> "$GITHUB_ENV"
  fi
fi
if [ "$ABORT" = true ]; then
  exit 1
fi
echo "Rate limit check passed"
