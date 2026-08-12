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
# Filters out the current run's own check run ($GITHUB_RUN_ID — in flight for
# as long as this loop runs) and sibling runs of the same workflow
# ($GITHUB_WORKFLOW — queued behind this run's concurrency group, so waiting
# on them deadlocks). Check runs are reduced to the latest per (check name,
# workflow): a concurrency-cancelled run's check runs stay on the commit
# forever, and an `if: always()` omnibus whose dependency was cancelled
# concludes FAILURE rather than CANCELLED, so without the reduction a settled
# green replacement still reads red. The reduction assumes the later entry
# supersedes the earlier — true for the cancellation cascade it targets,
# not for two independent runs of a group-less workflow; where that
# distinction must be exact, read the specific run's own conclusion.
#
# Exit codes:
#   0  every gating check on <sha> settled green
#   1  red — failing checks and their run URLs on stdout
#   2  no rollup for <sha> (unresolvable OID, or an ephemeral merge-ref
#      commit, which carries none) — UNVERIFIED, not green
#   3  poll cap hit with checks still pending — UNVERIFIED, not green

set -euo pipefail

PR="$1"
SHA="$2"
OWNER="${GITHUB_REPOSITORY%/*}"
NAME="${GITHUB_REPOSITORY#*/}"

# One call returns check runs *and* legacy status contexts. `pending` counts
# every non-terminal shape; `failed` counts every terminal-and-red conclusion
# — STARTUP_FAILURE and ACTION_REQUIRED are terminal CheckConclusionStates
# that land in neither bucket if forgotten, so a job that never started would
# read as green. CANCELLED stays excluded: a cancelled sibling is not a
# verdict on this commit. Empty output means no usable rollup — an errors
# envelope, an unresolvable OID, and a null rollup all land there, and none
# of them may read as settled green.
rollup() {
  local resp
  resp=$(gh api graphql \
    -f owner="$OWNER" -f name="$NAME" -f oid="$SHA" \
    -f query='
      query($owner: String!, $name: String!, $oid: GitObjectID!) {
        repository(owner: $owner, name: $name) {
          object(oid: $oid) {
            ... on Commit {
              statusCheckRollup {
                contexts(first: 100) {
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
  printf '%s' "$resp" | jq -c \
    --arg own "/runs/${GITHUB_RUN_ID:-}/" --arg wf "${GITHUB_WORKFLOW:-}" '
    .data.repository.object.statusCheckRollup
    | select(. != null)
    | [.contexts.nodes[]
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
       | select($wf == "" or .workflow != $wf)]
    | group_by([.name, .workflow]) | map(max_by(.startedAt))
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
  now_oid=$(gh pr view "$PR" --json headRefOid --jq '.headRefOid' 2>/dev/null) || now_oid=""
  if [ -n "$now_oid" ] && [ "$now_oid" != "$SHA" ]; then
    echo "note: branch advanced to $now_oid — the result above is still $SHA's, the commit this run is accountable for"
  fi
}

R=""
for _ in $(seq 1 9); do
  sleep 60
  R=$(rollup)
  [ -z "$R" ] && continue
  [ "$(printf '%s' "$R" | jq '.pending | length')" -gt 0 ] && continue
  # A just-settled matrix can register its `if: always()` omnibus a second or
  # two later; re-check once before trusting pending == 0.
  sleep 30
  R=$(rollup)
  [ -z "$R" ] && continue
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
  echo "no rollup returned for $SHA — UNVERIFIED, not green"
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
