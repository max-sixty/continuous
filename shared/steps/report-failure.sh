#!/usr/bin/env bash
# File or append to a `tend-outage` issue when a run fails, so outages are
# tracked until resolved. Shared verbatim by both harness actions; the
# caller gates it on the agent step having failed.
#
# Just records the run link. Error annotations and logs are not reliably
# available while the job is in_progress, so the nightly skill enriches these
# issues after the fact, when the run has completed and the APIs return stable
# data.
#
# A closed outage issue is left closed and a fresh one filed: closing it means
# the outage was resolved, and reopening would fold the next incident into a
# stale record. The rate-limit issue takes the opposite policy, for reasons in
# lib/run-issue.sh.
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
EXISTING=$(run_issue_canonical "$LABEL" open "$TITLE")

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
  printf '%s\n' "$ROW" | gh issue comment "$EXISTING" -F -

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
  sleep 5
  gh api --paginate --slurp \
    "repos/${GITHUB_REPOSITORY}/issues/${EXISTING}/comments?per_page=100" \
    | jq -r "add | [.[] | select(.body | contains(\"${ANCHOR}\"))] | sort_by(.created_at) | .[1:] | .[].id" \
    | while read -r DUP_ID; do
        [ -z "$DUP_ID" ] && continue
        gh api -X DELETE "repos/${GITHUB_REPOSITORY}/issues/comments/${DUP_ID}" 2>/dev/null || true
      done
else
  printf '%s\n\n%s\n\n%s\n' \
    "The bot failed to process a request. This issue tracks failures until the underlying cause is resolved." \
    "$ROW" \
    "This issue was created automatically. Close it once the outage is resolved." \
    | run_issue_create_and_reconcile "$LABEL" "$TITLE" > /dev/null
fi
