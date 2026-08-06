#!/usr/bin/env bash
# Shared preflight: abort if the bot is creating issues/PRs faster than a
# burst limit (20-minute window) or a daily spike limit (vs a 6-day baseline).
# Shared verbatim by both harness actions.
#
# The spike limit is resumable. When it trips, the run files or reopens a
# `tend-rate-limit` issue naming the run it refused, and a maintainer closing
# that issue doubles the ceiling for the rest of the UTC day; each further
# close doubles it again. Two things follow from that shape:
#
#   - Opening the issue is the notice. A bare `::error::` annotation lands on
#     a job nobody opens, so before this the repo could stop reviewing for
#     hours with no signal but a red check on unrelated PRs.
#   - Closes by the bot itself do not count, which is what makes this a check
#     rather than an instruction the bot could be talked out of. GitHub lets
#     only the author or a triage/write collaborator close an issue, and the
#     bot is the author — so excluding the bot leaves exactly the maintainers,
#     with no allowlist to keep.
#
# A doubling rather than a flat increment because the ceiling it lifts is
# itself proportional (`10 + 2 * daily_avg`): a repo filing 15 a day would
# need a close every hour to get through a legitimate spike on a flat bump.
# At the formula's floor the two coincide — 10 doubles to 20 — so they only
# diverge where a flat bump stops working.
#
# The burst limit is deliberately not resumable: ten PRs in twenty minutes is
# a loop rather than a busy day, and there is nothing there to wave through.
#
# Inputs (env): GITHUB_TOKEN (the bot's PAT, for gh), GITHUB_REPOSITORY (from
# Actions), plus the run/event vars lib/run-issue.sh reads.
set -eo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/run-issue.sh
. "${SCRIPT_DIR}/lib/run-issue.sh"

REPO="${GITHUB_REPOSITORY}"
PAUSE_LABEL="tend-rate-limit"
PAUSE_TITLE="Bot rate limit reached"

# Who the bot is comes from the credential, not from configuration: this runs
# as the bot, so the authenticated user is the bot by definition. A configured
# name could be misspelled or left stale by a rename, and every way that went
# wrong failed open — the counts below would match nothing and never trip, and
# the approval filter would match every close including the bot's own. Neither
# is possible when the name and id are read off the token.
#
# The id is what the approval filter compares, because the bot account is an
# ordinary user account rather than a GitHub App: the type check further down
# does nothing for it, so identifying it is the whole control.
read -r BOT BOT_ID <<< "$(gh api user --jq '"\(.login) \(.id)"')"
if [ -z "$BOT" ] || [ -z "$BOT_ID" ]; then
  echo "::error::Rate limit preflight: could not read the bot's own identity from the token, so the limit cannot be enforced."
  exit 1
fi
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
#
# The bot's own bookkeeping issues (`tend-outage`, `tend-rate-limit`) are
# counted like anything else it files. A flood of them is itself a plausible
# runaway — a bot stuck in a failure loop files them — so excluding them would
# blind the metric to one of the shapes it exists to catch.
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

# Everything below runs only once the base limit is already exceeded, so the
# common path costs no extra API calls at all.
if [ "$TODAY_POSTS" -gt "$SPIKE_LIMIT" ]; then
  PAUSE=$(run_issue_canonical "$PAUSE_LABEL" all)

  # Approvals are closes by a person, today. Scoped to today because the
  # ceiling they lift resets at the UTC rollover along with the count itself.
  #
  # Two exclusions, covering different things. `id != BOT_ID` rules out the
  # bot. `type != "Bot"` rules out GitHub Apps — `github-actions[bot]` above
  # all, which a workflow's own `GITHUB_TOKEN` acts as, and which is a
  # different account that would otherwise read as a person. Excluding the
  # class rather than enumerating app names keeps this from needing an
  # allowlist.
  #
  # Counted with `wc -l` outside jq rather than a `length` inside it: under
  # `--paginate` a reduce runs once per page and prints one number per page,
  # so `| length` would silently report the last page's count.
  APPROVALS=0
  if [ -n "$PAUSE" ]; then
    # Counted in two steps rather than one pipeline: under `pipefail` a
    # transient failure of the API call would take the whole script down here,
    # losing the issue that is the only notice anyone gets. Failing to read
    # the timeline defaults to no approvals, which refuses the run — the safe
    # direction for a check whose job is to stop things.
    APPROVAL_ACTORS=$(gh api "repos/$REPO/issues/$PAUSE/timeline" --paginate \
      --jq ".[] | select(.event == \"closed\"
                         and .actor.id != ${BOT_ID}
                         and .actor.type != \"Bot\"
                         and .created_at >= \"${TODAY}T00:00:00Z\") | .actor.login" \
      || true)
    if [ -n "$APPROVAL_ACTORS" ]; then
      APPROVALS=$(printf '%s\n' "$APPROVAL_ACTORS" | wc -l | tr -d ' ')
    fi
    # Only so the shift below cannot run off a 64-bit integer into a negative
    # ceiling. Twelve closes in one day is already far past anything real.
    if [ "$APPROVALS" -gt 12 ]; then
      APPROVALS=12
    fi
  fi

  CEILING=$((SPIKE_LIMIT << APPROVALS))

  if [ "$TODAY_POSTS" -gt "$CEILING" ]; then
    run_issue_ensure_label "$PAUSE_LABEL" "Bot paused on its own rate limit; close to approve" "fbca04"
    ROW=$(run_issue_row)

    # Same jittered check-then-act as report-failure.sh, for the same reason:
    # sibling jobs trip within seconds of each other.
    sleep $((RANDOM % 30))
    EXISTING=$(run_issue_canonical "$PAUSE_LABEL" all)

    if [ -n "$EXISTING" ]; then
      # A no-op when it is already open, which is the usual case for the
      # second and later runs refused in one incident.
      gh issue reopen "$EXISTING" 2>/dev/null || true
      printf '%s\n' "$ROW" > /tmp/rate-limit-row.md
      gh issue comment "$EXISTING" -F /tmp/rate-limit-row.md
      PAUSE="$EXISTING"
    else
      printf '%s\n\n%s\n\n%s\n\n%s\n' \
        "The bot stopped before doing any work: it has filed more issues and PRs today than its spike limit allows, which is the check that catches a runaway loop between workflows." \
        "**Closing this issue approves the volume and doubles the ceiling for the rest of the UTC day.** Each further close doubles it again, so the limit keeps working after you have used it. Close it only if the activity below is expected — and note the bot cannot approve itself: closes by its own account, or by any GitHub App, are not counted." \
        "$ROW" \
        "The runs listed above were refused and do not retry on their own; re-run them with \`gh run rerun <id> --failed\` once this is closed." > /tmp/rate-limit-body.md
      run_issue_create_and_reconcile "$PAUSE_LABEL" "$PAUSE_TITLE" /tmp/rate-limit-body.md
      PAUSE=$(run_issue_canonical "$PAUSE_LABEL" open)
    fi

    echo "::error::Rate limit: bot created ${TODAY_POSTS} items today, above the ceiling of ${CEILING} (base limit ${SPIKE_LIMIT}, ${APPROVALS} approval(s), baseline ${PAST_POSTS} over past 6 days). Refused runs are listed in #${PAUSE:-?}; closing it doubles the ceiling."
    ABORT=true
  else
    echo "Rate limit: ${TODAY_POSTS} today is over the base limit of ${SPIKE_LIMIT}, allowed by ${APPROVALS} approval(s) on #${PAUSE} (ceiling ${CEILING})"
  fi
fi

if [ "$ABORT" = true ]; then
  exit 1
fi
echo "Rate limit check passed"
