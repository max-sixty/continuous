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
if [ -z "$BOT_ID" ]; then
  echo "::error::Rate limit preflight: could not read the bot's own identity from the token, so the limit cannot be enforced."
  exit 1
fi
# Published for the rest of the job, which needs the same two facts and would
# otherwise resolve them again from the configured name.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'login=%s\nid=%s\n' "$BOT" "$BOT_ID" >> "$GITHUB_OUTPUT"
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
  PAUSE=$(run_issue_canonical "$PAUSE_LABEL" all "$PAUSE_TITLE")

  APPROVALS=0
  if [ -n "$PAUSE" ]; then
    # `/events` rather than `/timeline`: it carries every field read here and
    # excludes comments, which matter because this issue accumulates one per
    # refused run and is never replaced. Captured whole and counted in bash,
    # because under `pipefail` a transient failure mid-pipeline would take the
    # script down and lose the issue that is the only notice anyone gets;
    # failing to read the events leaves approvals at zero, which refuses the
    # run — the safe direction for a check whose job is to stop things.
    EVENTS=$(gh api "repos/$REPO/issues/$PAUSE/events?per_page=100" --paginate \
      --jq ".[] | select((.event == \"labeled\" and .label.name == \"${PAUSE_LABEL}\")
                      or (.event == \"closed\"
                          and .actor.id != ${BOT_ID}
                          and .actor.type != \"Bot\")) | \"\(.event) \(.created_at)\"" \
      || true)

    # An approval is a close by a person, after the label went on and today.
    #
    # Today, because the ceiling it lifts resets at the UTC rollover along
    # with the count itself. After the label, because otherwise moving the
    # label onto an issue closed at any earlier point would import that close
    # as an approval — and the bot can move labels. On the issue the preflight
    # files, the label goes on at creation, so this excludes nothing real.
    #
    # The two actor exclusions cover different things: `id != BOT_ID` rules
    # out the bot, and `type != "Bot"` rules out GitHub Apps —
    # `github-actions[bot]` above all, which a workflow's own `GITHUB_TOKEN`
    # acts as, and which is a different account that would otherwise read as a
    # person. Excluding the class rather than naming apps needs no allowlist.
    SINCE="${TODAY}T00:00:00Z"
    while read -r KIND AT; do
      if [ "$KIND" = "labeled" ] && [[ "$AT" > "$SINCE" ]]; then
        SINCE="$AT"
      fi
    done <<< "$EVENTS"

    while read -r KIND AT; do
      if [ "$KIND" = "closed" ] && [[ "$AT" > "$SINCE" ]]; then
        APPROVALS=$((APPROVALS + 1))
      fi
    done <<< "$EVENTS"
  fi

  CEILING=$((SPIKE_LIMIT << APPROVALS))

  if [ "$TODAY_POSTS" -le "$CEILING" ]; then
    echo "Rate limit: ${TODAY_POSTS} today is over the base limit of ${SPIKE_LIMIT}, allowed by ${APPROVALS} approval(s) on #${PAUSE} (ceiling ${CEILING})"
  else
    # Only file when the spike is the whole reason this run is being refused.
    # A burst trip is not resumable, so an issue offering to lift the ceiling
    # would promise a recovery that closing it cannot deliver; the burst
    # annotation above is the honest signal, and no row is owed for a run
    # whose retry would be refused again on the same grounds.
    if [ "$ABORT" = false ]; then
      ROW=$(run_issue_row)

      # Only look again when there is nothing to append to. The jitter narrows
      # the create-create race — sibling jobs trip within seconds of each
      # other, and without it each files its own issue — so it buys nothing
      # once the issue is known to exist, and the lookup above is still good.
      if [ -z "$PAUSE" ]; then
        sleep $((RANDOM % 30))
        PAUSE=$(run_issue_canonical "$PAUSE_LABEL" all "$PAUSE_TITLE")
      fi

      if [ -n "$PAUSE" ]; then
        # A no-op when it is already open, which is the usual case for the
        # second and later runs refused in one incident.
        gh issue reopen "$PAUSE" >/dev/null 2>&1 || true
        printf '%s\n' "$ROW" | gh issue comment "$PAUSE" -F -
      else
        run_issue_ensure_label "$PAUSE_LABEL" "Bot paused on its own rate limit; close to approve" "fbca04"
        PAUSE=$(printf '%s\n\n%s\n\n%s\n\n%s\n' \
          "The bot stopped before doing any work: it has filed more issues and PRs today than its spike limit allows, which is the check that catches a runaway loop between workflows." \
          "**Closing this issue approves the volume and doubles the ceiling for the rest of the UTC day.** Each further close doubles it again, so the limit keeps working after you have used it. Close it only if the activity below is expected — and note the bot cannot approve itself: closes by its own account, or by any GitHub App, are not counted." \
          "$ROW" \
          "The runs listed above were refused and do not retry on their own; re-run them with \`gh run rerun <id> --failed\` once this is closed." \
          | run_issue_create_and_reconcile "$PAUSE_LABEL" "$PAUSE_TITLE")
      fi

      echo "::error::Rate limit: bot created ${TODAY_POSTS} items today, above the ceiling of ${CEILING} (base limit ${SPIKE_LIMIT}, ${APPROVALS} approval(s), baseline ${PAST_POSTS} over past 6 days). Refused runs are listed in #${PAUSE:-?}; closing it doubles the ceiling."
    else
      echo "::error::Rate limit: bot created ${TODAY_POSTS} items today, above the ceiling of ${CEILING} (base limit ${SPIKE_LIMIT}, ${APPROVALS} approval(s), baseline ${PAST_POSTS} over past 6 days)."
    fi
    ABORT=true
  fi
fi

if [ "$ABORT" = true ]; then
  exit 1
fi
echo "Rate limit check passed"
