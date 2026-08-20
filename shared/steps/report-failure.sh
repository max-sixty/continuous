#!/usr/bin/env bash
# File or append to a `tend-outage` issue when a run fails, so outages are
# tracked until resolved. Shared verbatim by both harness actions; the caller
# gates it on the job being red, so a failure anywhere in the action — the
# rate-limit preflight and sandbox build ahead of the agent as much as the
# agent itself — lands in the tracker. The one exclusion is the security
# preflight: that failure is a persistent config refusal rather than an
# outage, and the issue this files ("temporarily unavailable", closed once
# resolved) would never resolve.
#
# Just records the run link. Error annotations and logs are not reliably
# available while the job is in_progress, so the nightly skill enriches these
# issues after the fact, when the run has completed and the APIs return stable
# data.
#
# A closed outage issue is left closed and a fresh one filed: closing it means
# the `review-runs` drain step read every row and re-ran what needed it, and
# reopening would fold the next incident into a stale record. That drain owns
# the close — the nightly skill's resolved-issue rule carves this label out,
# because nightly's cron precedes review-runs' and "nothing has failed since"
# says nothing about the stranded triggers the rows name. Where an adopter
# disables `review-runs`, nothing substitutes for it and the close falls to a
# maintainer against the same criterion — which is why the issue body states
# the criterion rather than naming the sweep as the only route. The rate-limit
# issue takes the opposite policy, for reasons in lib/run-issue.sh.
#
# Inputs (env): GITHUB_TOKEN (for gh), GITHUB_SERVER_URL, GITHUB_REPOSITORY,
# GITHUB_RUN_ID, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH (from Actions).
set -eo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/run-issue.sh
. "${SCRIPT_DIR}/lib/run-issue.sh"

LABEL="tend-outage"
TITLE="Bot temporarily unavailable"

ROW=$(run_issue_row)
ANCHOR=$(run_issue_anchor)

run_issue_ensure_label "$LABEL" "Tracks bot outage incidents" "d93f0b"

# Jittered backoff before the check-then-act narrows the race window when a
# matrix workflow's legs fail at near-identical times (e.g. model-API 5xx
# responses exhausting the retry budget across every leg within a few
# seconds). Without this, every leg reads $EXISTING as empty in parallel and
# each files its own outage issue.
sleep $((RANDOM % 30))
# A failed read is not "nothing is open". Filing on it is how a repo ends up
# with two open trackers, and the reconcile does not clean this one up — it
# probes the ten numbers below the issue it just filed, and an already-open
# tracker is normally much older. Two of them scatter later rows across both,
# so no tracker carries the complete set the drain sweep needs. Skipping costs
# this one row on a transient failure, and the next failure records normally.
if ! EXISTING=$(run_issue_canonical "$LABEL" open "$TITLE"); then
  echo "::warning::Could not read this repo's ${LABEL} issues, so this run was not recorded on the outage tracker."
  exit 0
fi

if [ -n "$EXISTING" ]; then
  # Per-run comment dedup. A matrix workflow invokes this script once per leg,
  # every leg sharing one GITHUB_RUN_ID. Without a guard each leg appends its
  # own near-identical row and floods the issue — a 5-leg matrix failing during
  # an outage leaves 5 comments all citing the same run. Skip if this run is
  # already recorded, whether in the issue body (a leg of this same run seeded
  # the issue) or in an existing comment.
  #
  # Read into a variable and match in the shell rather than piping to `grep
  # -q`: under `pipefail` grep's exit-on-first-match closes the pipe, and the
  # SIGPIPE that `gh` then takes becomes the pipeline's status. That reads as
  # "not recorded" on any issue whose text outruns the pipe buffer — i.e. the
  # flooded ones this guard exists for. A read that outright fails falls
  # through to posting, which risks a duplicate row rather than losing one.
  RECORDED=$(gh issue view "$EXISTING" --json body,comments \
    --jq '.body + "\n" + ([.comments[].body] | join("\n"))' || true)
  case "$RECORDED" in
    *"$ANCHOR"*)
      echo "Run ${GITHUB_RUN_ID} already recorded on #${EXISTING} — skipping duplicate comment"
      exit 0
      ;;
  esac

  # The common path once a tracker is open — every failure after the first in
  # one incident appends through here — and it can 5xx like any other write.
  # Left bare it aborts under `set -e`, which drops the row without saying so:
  # the tracker then under-reports the outage, and a run stranded by it reads
  # as one that never happened. Warning costs the same single row a failed
  # read above costs, and the next failure records normally. The create below
  # keeps the opposite policy deliberately: with no tracker open there is no
  # other record of the outage, so a failed create has to redden the step.
  # The rate-limit caller guards both of its writes instead: a failed create
  # there must still reach the annotation that names what to close, where here
  # the create is the last statement and the red step is all that is left.
  # A leg that failed to post has nothing of its own to reconcile, so it stops
  # here; any racing leg that did post reconciles on its own pass.
  if ! printf '%s\n' "$ROW" | gh issue comment "$EXISTING" -F -; then
    echo "::warning::Could not append this run's row to #${EXISTING}."
    exit 0
  fi

  # The check-then-act above still races across concurrently-jittered legs: two
  # can both read no matching row before either posts. Reconcile to one row per
  # run — keep the earliest comment carrying this run's anchor, delete the rest.
  # Convergent, like the create reconcile in run_issue_create_and_reconcile:
  # every racing leg sorts the same way and computes the same keeper, so
  # deleting an already-deleted comment is a harmless 404. Selecting on the
  # anchor keeps only the bot's own generated rows eligible for deletion.
  # Paginated, because this endpoint returns comments oldest-first and the
  # issues that need reconciling are exactly the flooded ones: past 100
  # comments the rows just posted fall off the first page, and an unpaginated
  # read would find nothing to reconcile on the only issues where it matters.
  # `--paginate` alone won't do — `gh` applies `--jq` per page, so each page
  # would keep its own earliest comment and `sort_by | .[1:]` would delete the
  # keeper. `--slurp` refuses `--jq`, hence the filter moved downstream, with
  # `add` flattening the array of pages. On an issue with no comments `--slurp`
  # yields `[[]]`, so `add` gives `[]` and the filter emits nothing.
  #
  # Guarded like the append above, and for the same reason: this is the last
  # statement in the branch, so a 5xx on the read would take the script's exit
  # status with it *after* the row landed — a red step carrying no annotation
  # to say why, on the job someone is about to diagnose. Best-effort cleanup
  # failing leaves duplicate rows on the tracker, which is the cheaper loss.
  sleep 5
  gh api --paginate --slurp \
    "repos/${GITHUB_REPOSITORY}/issues/${EXISTING}/comments?per_page=100" \
    | jq -r "add | [.[] | select(.body | contains(\"${ANCHOR}\"))] | sort_by(.created_at) | .[1:] | .[].id" \
    | while read -r DUP_ID; do
        [ -z "$DUP_ID" ] && continue
        gh api -X DELETE "repos/${GITHUB_REPOSITORY}/issues/comments/${DUP_ID}" 2>/dev/null || true
      done || echo "::warning::Could not reconcile duplicate rows on #${EXISTING}; this run's row is recorded."
else
  printf '%s\n\n%s\n\n%s\n' \
    "The bot failed to process a request. This issue tracks failures until the underlying cause is resolved." \
    "$ROW" \
    "This issue was created automatically. Close it once every row above is drained — each stranded trigger re-run, or confirmed no longer needed. The \`tend-review-runs\` sweep does that where it runs." \
    | run_issue_create_and_reconcile "$LABEL" "$TITLE" "$ROW" > /dev/null
fi
