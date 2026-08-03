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
# Inputs (env): GITHUB_TOKEN (for gh), GITHUB_SERVER_URL, GITHUB_REPOSITORY,
# GITHUB_RUN_ID, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH (from Actions).
set -eo pipefail

LABEL="tend-outage"
TITLE="Bot temporarily unavailable"
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

# Build a one-line reference to the triggering context
REF=""
if [ "$GITHUB_EVENT_NAME" = "pull_request_target" ] || [ "$GITHUB_EVENT_NAME" = "pull_request_review" ] || [ "$GITHUB_EVENT_NAME" = "pull_request_review_comment" ]; then
  PR_NUM=$(jq -r '.pull_request.number' "$GITHUB_EVENT_PATH")
  REF="#${PR_NUM}"
elif [ "$GITHUB_EVENT_NAME" = "issues" ] || [ "$GITHUB_EVENT_NAME" = "issue_comment" ]; then
  ISSUE_NUM=$(jq -r '.issue.number' "$GITHUB_EVENT_PATH")
  REF="#${ISSUE_NUM}"
elif [ "$GITHUB_EVENT_NAME" = "workflow_run" ]; then
  REF="CI fix for workflow run"
fi

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# One row per failure, in the same table format whether it seeds the issue
# body (first failure) or is appended as a comment (every later failure), so
# both render identically.
TABLE=$(printf '%s\n%s\n%s' \
  "| When | Run | Trigger |" \
  "|------|-----|---------|" \
  "| ${TIMESTAMP} | [workflow run](${RUN_URL}) | ${REF:-N/A} |")

gh label create "$LABEL" --description "Tracks bot outage incidents" --color "d93f0b" 2>/dev/null || true

# Jittered backoff before the check-then-act narrows the race window
# when a matrix workflow's legs fail at near-identical times (e.g.
# model-API 5xx responses exhausting the retry budget across every leg
# within a few seconds). Without this, every leg reads $EXISTING as empty
# in parallel and each files its own outage issue.
sleep $((RANDOM % 30))
EXISTING=$(gh issue list --label "$LABEL" --state open --json number --jq '.[0].number // empty')

if [ -n "$EXISTING" ]; then
  # Per-run comment dedup. A matrix workflow invokes this script once per leg,
  # every leg sharing one GITHUB_RUN_ID (and thus one RUN_URL). Without a guard
  # each leg appends its own near-identical row and floods the issue (a 5-leg
  # matrix failing during an outage → 5 comments all citing the same run).
  # Skip if this run is already recorded — whether in the issue body (a leg of
  # this same run seeded the issue) or in an existing comment.
  if gh issue view "$EXISTING" --json body,comments \
      --jq '.body + "\n" + ([.comments[].body] | join("\n"))' \
      | grep -qF "$RUN_URL"; then
    echo "Run ${GITHUB_RUN_ID} already recorded on #${EXISTING} — skipping duplicate comment"
    exit 0
  fi
  printf '%s\n' "$TABLE" > /tmp/comment.md
  gh issue comment "$EXISTING" -F /tmp/comment.md

  # The check-then-act above still races across concurrently-jittered legs: two
  # can both read no matching row before either posts. Reconcile to one row per
  # run — keep the earliest comment citing this RUN_URL, delete later dups.
  # Convergent, mirroring the issue reconcile below: every racing leg sorts the
  # same way and computes the same keeper, so deleting an already-deleted
  # comment is a harmless 404.
  sleep 5
  gh api "repos/${GITHUB_REPOSITORY}/issues/${EXISTING}/comments?per_page=100" \
    --jq "[.[] | select(.body | contains(\"${RUN_URL}\"))] | sort_by(.created_at) | .[1:] | .[].id" \
    | while read -r DUP_ID; do
        [ -z "$DUP_ID" ] && continue
        gh api -X DELETE "repos/${GITHUB_REPOSITORY}/issues/comments/${DUP_ID}" 2>/dev/null || true
      done
else
  printf '%s\n\n%s\n\n%s\n' \
    "The bot failed to process a request. This issue tracks failures until the underlying cause is resolved." \
    "$TABLE" \
    "This issue was created automatically. Close it once the outage is resolved." > /tmp/body.md
  gh issue create --title "$TITLE" --label "$LABEL" -F /tmp/body.md

  # The jitter above only narrows the create-create race; it can't close it.
  # Two legs can still both read $EXISTING empty (jitter collision within the
  # few-second window the list index takes to reflect a fresh create), so each
  # files its own issue. Reconcile after creating: settle for the index, list
  # every open tend-outage issue, keep the lowest-numbered, and close the rest
  # as duplicates. Idempotent and convergent — every racing leg computes the
  # same keeper, so a second leg closing an already-closed dup is a no-op.
  sleep 5
  OPEN=$(gh issue list --label "$LABEL" --state open --json number --jq 'sort_by(.number) | .[].number')
  KEEP=$(echo "$OPEN" | head -1)
  echo "$OPEN" | tail -n +2 | while read -r DUP; do
    [ -z "$DUP" ] && continue
    gh issue close "$DUP" \
      --comment "Duplicate of #${KEEP} (concurrent matrix-leg failure); consolidating outage tracking there." \
      2>/dev/null || true
  done
fi
