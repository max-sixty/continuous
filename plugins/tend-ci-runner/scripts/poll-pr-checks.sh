#!/usr/bin/env bash
# Polls the status-check rollup of a specific commit until every check that
# can gate a follow-up is terminal, then reports the verdict.
#
# Usage: poll-pr-checks.sh <pr-number> <sha>
#
# The rollup is queried by commit OID, never through the PR: the PR's current
# head moves whenever another actor pushes, and a poll that follows it exits
# when *their* commit settles, reporting results this session's push never
# earned. <sha> is the commit this session is accountable for.
#
# The rollup is the only trustworthy poll signal: `gh pr checks --required`
# lists only contexts already registered on the commit (an `if: always()`
# omnibus registers after its whole `needs:` matrix settles, so an early poll
# reads green with the matrix invisible), and mergeStateStatus's BLOCKED is a
# catch-all — pending checks, missing reviews, out-of-date branch —
# indistinguishable without admin scope.
#
# Filters out the current run's own check run ($GITHUB_RUN_ID — in flight for
# as long as this loop runs), sibling runs of the same workflow
# ($GITHUB_WORKFLOW — queued behind this run's concurrency group, so waiting
# on them deadlocks), and every other generated `tend-*` workflow bar
# `tend-install-test`. No tend *agent* job is a verdict on the code — red ones
# included, which are tend outages the caller cannot act on from here — and
# `tend-review` fires on the very `synchronize` this poll is verifying, so its
# agent job starts seconds after the loop and routinely outlives the cap.
# Without this clause every session that pushes to a PR reports UNVERIFIED with
# every repo check already green. `tend-install-test` is the one generated
# workflow that runs no agent: it regenerates the committed workflow files and
# diffs them, so a red one is drift in the code under poll and has to gate. The
# generator hardcodes both names, so the match is stable rather than a guess
# about adopter naming, and it is keyed on the workflow — a repo's own job
# named `review` still gates. `test_poll_exemptions_match_the_agentless_generated_workflows`
# pins the two sets as complements, so a second agentless generated workflow
# goes red there rather than inheriting the exemption and reading green here.
#
# Filtering to empty is not green. A rollup holding only exempt entries answers
# nothing about this commit — the same state as no rollup at all — and this
# exemption makes that routine, since `tend-review` registers within seconds of
# the push while the repo's own checks may not have. The reduction emits
# nothing in that case, so the loop keeps polling and the cap reports
# UNVERIFIED rather than green.
#
# Check runs are grouped per (check name, workflow): a
# group with any non-terminal entry is pending, and a fully-terminal group is
# judged by its latest entry — a concurrency-cancelled run's check runs stay
# on the commit forever, and an `if: always()` omnibus whose dependency was
# cancelled concludes FAILURE rather than CANCELLED, so without the reduction
# a settled green replacement still reads red. Latest-wins assumes the later
# entry supersedes the earlier — true for the cancellation cascade it
# targets, not for two independent runs of a group-less workflow; where that
# distinction must be exact, read the specific run's own conclusion.
#
# Exit codes:
#   0  every gating check on <sha> settled green
#   1  red — failing checks and their run URLs on stdout
#   2  no usable rollup for <sha> — UNVERIFIED, not green. A <sha> that isn't
#      a full 40-char lowercase OID, rejected at entry; an unresolvable OID; an
#      ephemeral merge-ref commit, which carries none; a commit with
#      zero checks and zero statuses (a push every workflow's paths filter
#      excludes — the rollup is null then too, so "nothing to gate on" is
#      indistinguishable from "nothing answered"); a rollup whose every entry
#      the filters above exempted, which reaches that same state by another
#      route; or a page walk that could not be finished, where an unread node
#      could hide a failure
#   3  poll cap hit with checks still pending — UNVERIFIED, not green

set -euo pipefail

# Both default rather than relying on `set -u`: an omitted <sha> is the likelier
# arity mistake, and unbound-variable death exits 1 — the code documented above
# as red, sending the caller off to diagnose a CI failure that never happened.
# Defaulting routes a short call into the guard below instead.
PR="${1:-}"
SHA="${2:-}"
OWNER="${GITHUB_REPOSITORY%/*}"

# GraphQL's `GitObjectID!` rejects an abbreviated OID at coercion time, which
# rollup() cannot tell from a transient failure: without this the loop sleeps
# through its whole budget before reporting a caller bug, and head_note's
# string compare then claims the branch advanced to the very commit that was
# passed in. Lowercase-only for the same reason from the other direction:
# GraphQL coerces an uppercase OID happily, but head_note compares it against
# `headRefOid`, which comes back lowercase — so an uppercase argument
# mismatches its own commit and trails every verdict with that spurious note.
# Nothing here emits uppercase (`git rev-parse` and `headRefOid` are both
# lowercase), so rejecting it costs no real caller. head_note can still
# misfire on the other exit-2 causes above — an unresolvable OID and a
# merge-ref commit are both full-length and neither is the branch head — so
# this narrows that note rather than fixing it.
if [[ ! $SHA =~ ^[0-9a-f]{40}$ ]]; then
  echo "poll-pr-checks.sh: <sha> must be a full 40-char lowercase commit OID, got '$SHA' — UNVERIFIED, not green" >&2
  exit 2
fi
NAME="${GITHUB_REPOSITORY#*/}"

# One query returns check runs *and* legacy status contexts, a page at a
# time. The pages are walked to exhaustion rather than sampled: a full matrix
# passes 100 contexts routinely — a dependency bump touching a lockfile opens
# every path filter at once — and the failing check can sit on any page.
# `pending` counts every non-terminal shape; `failed` counts every
# terminal-and-red conclusion — STARTUP_FAILURE and ACTION_REQUIRED are
# terminal CheckConclusionStates that land in neither bucket if forgotten, so
# a job that never started would read as green. CANCELLED stays excluded: a
# cancelled sibling is not a verdict on this commit. Empty output means no
# usable rollup — an errors envelope, an unresolvable OID, a null rollup, a
# rollup every entry of which was exempted above, and a pagination that could
# not be finished all land there, and none of them may read as settled green.
rollup() {
  local resp page nodes cursor
  local -a cursor_arg
  nodes='[]'
  cursor=""
  while :; do
    # `-F` types the literal `null` as JSON null for the first page, so
    # `after:` is a no-op there; later pages pass the cursor as a raw string.
    if [ -z "$cursor" ]; then cursor_arg=(-F cursor=null); else cursor_arg=(-f cursor="$cursor"); fi
    resp=$(gh api graphql \
      -f owner="$OWNER" -f name="$NAME" -f oid="$SHA" "${cursor_arg[@]}" \
      -f query='
        query($owner: String!, $name: String!, $oid: GitObjectID!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            object(oid: $oid) {
              ... on Commit {
                statusCheckRollup {
                  contexts(first: 100, after: $cursor) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      __typename
                      ... on CheckRun {
                        name status conclusion startedAt detailsUrl
                        checkSuite { workflowRun { workflow { name } } }
                      }
                      ... on StatusContext { context state targetUrl }
                    }
                  }
                }
              }
            }
          }
        }' 2>/dev/null) || return 0
    page=$(printf '%s' "$resp" | jq -c \
      '.data.repository.object.statusCheckRollup.contexts | select(. != null)' \
      2>/dev/null) || return 0
    [ -z "$page" ] && return 0
    nodes=$(printf '%s' "$page" | jq -c --argjson acc "$nodes" '$acc + .nodes') || return 0
    # A truncated walk is no rollup at all: `hasNextPage` with no cursor to
    # follow would otherwise re-read page one forever.
    [ "$(printf '%s' "$page" | jq -r '.pageInfo.hasNextPage')" = "true" ] || break
    cursor=$(printf '%s' "$page" | jq -r '.pageInfo.endCursor // ""')
    [ -z "$cursor" ] && return 0
  done
  # A group with any non-terminal entry is pending regardless of timestamps:
  # a QUEUED replacement's startedAt cannot be relied on, and losing the
  # reduction to a stale settled entry would report a superseded verdict.
  printf '%s' "$nodes" | jq -c \
    --arg own "/runs/${GITHUB_RUN_ID:-}/" --arg wf "${GITHUB_WORKFLOW:-}" '
    [.[]
     | if .__typename == "CheckRun" then
         {name, status, conclusion: (.conclusion // ""),
          workflow: (.checkSuite.workflowRun.workflow.name // ""),
          url: (.detailsUrl // ""), startedAt: (.startedAt // "")}
       else
         {name: .context,
          status: (if .state == "PENDING" or .state == "EXPECTED"
                   then "PENDING" else "COMPLETED" end),
          conclusion: .state, workflow: "", url: (.targetUrl // ""),
          startedAt: ""}
       end
     | select(.url | test($own) | not)
     | select($wf == "" or .workflow != $wf)
     | select(.workflow == "tend-install-test"
              or (.workflow | startswith("tend-") | not))]
    | group_by([.name, .workflow])
    | map(if any(.[]; .status != "COMPLETED")
          then first(.[] | select(.status != "COMPLETED"))
          else max_by(.startedAt) end)
    | select(length > 0)
    | {pending: [.[] | select(.status != "COMPLETED") | .name],
       failed: [.[] | select(.status == "COMPLETED")
                | select(.conclusion == "FAILURE" or .conclusion == "TIMED_OUT"
                         or .conclusion == "STARTUP_FAILURE"
                         or .conclusion == "ACTION_REQUIRED"
                         or .conclusion == "ERROR")
                | "\(.name) \(.url)"]}' 2>/dev/null || true
}

# A moved head is reported as a distinct outcome, never silently absorbed:
# the verdict above stays <sha>'s.
head_note() {
  local now_oid
  now_oid=$(gh pr view "$PR" --repo "$GITHUB_REPOSITORY" --json headRefOid --jq '.headRefOid' 2>/dev/null) || now_oid=""
  if [ -n "$now_oid" ] && [ "$now_oid" != "$SHA" ]; then
    echo "note: branch advanced to $now_oid — the result above is still $SHA's, the commit this run is accountable for"
  fi
}

# R keeps the last usable rollup, so a transient blip on a later iteration
# doesn't discard what earlier polls saw — the cap report below still names
# the pending checks.
R=""
for _ in $(seq 1 9); do
  sleep 60
  cur=$(rollup)
  [ -z "$cur" ] && continue
  R="$cur"
  [ "$(printf '%s' "$R" | jq '.pending | length')" -gt 0 ] && continue
  # A just-settled matrix can register its `if: always()` omnibus a second or
  # two later; re-check once before trusting pending == 0.
  sleep 30
  cur=$(rollup)
  [ -z "$cur" ] && continue
  R="$cur"
  [ "$(printf '%s' "$R" | jq '.pending | length')" -gt 0 ] && continue

  if [ "$(printf '%s' "$R" | jq '.failed | length')" -gt 0 ]; then
    echo "red on $SHA:"
    printf '%s' "$R" | jq -r '.failed[]'
    head_note
    exit 1
  fi
  echo "green: every gating check on $SHA settled green"
  head_note
  exit 0
done

if [ -z "$R" ]; then
  echo "no gating check settled on $SHA — UNVERIFIED, not green"
  head_note
  exit 2
fi
echo "poll cap hit — still pending on $SHA (UNVERIFIED, not green):"
printf '%s' "$R" | jq -r '.pending[]'
if [ "$(printf '%s' "$R" | jq '.failed | length')" -gt 0 ]; then
  echo "failures observed so far (unconfirmed while checks pend):"
  printf '%s' "$R" | jq -r '.failed[]'
fi
head_note
exit 3
