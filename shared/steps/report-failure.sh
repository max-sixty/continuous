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
  printf '%s\n' "$TABLE" > /tmp/comment.md
  gh issue comment "$EXISTING" -F /tmp/comment.md
else
  printf '%s\n\n%s\n\n%s\n' \
    "The bot failed to process a request. This issue tracks failures until the underlying cause is resolved." \
    "$TABLE" \
    "This issue was created automatically. Close it once the outage is resolved." > /tmp/body.md
  MINE=$(basename "$(gh issue create --title "$TITLE" --label "$LABEL" -F /tmp/body.md)")
  echo "Filed outage issue #${MINE}"

  # The jitter above only narrows the create-create race; it can't close it.
  # Two legs can still both read $EXISTING empty (jitter collision within the
  # few-second window the list index takes to reflect a fresh create), so each
  # files its own issue and the pair has to be reconciled after the fact.
  #
  # Reconcile by probing issue numbers directly rather than re-listing. A
  # settle-then-list reconcile reads the same lagging index that lost the race
  # in the first place: observed in practice, a sibling created 3s earlier was
  # still absent from the list while one created 6s earlier was present, so two
  # legs whose creates landed in the same second each read back only their own
  # issue and closed nothing. `GET /issues/{n}` is a primary-key read and
  # returns a sibling the instant it exists, so no settle is needed.
  #
  # Issue numbers are monotonic, so any racing sibling sits just below ours:
  # scan a short window downwards and defer to the lowest open tracker found.
  # Convergent — every leg computes the same keeper from its own vantage point,
  # and only higher-numbered legs close themselves.
  KEEP=""
  for n in $(seq $((MINE - 1)) -1 $((MINE - 10))); do
    [ "$n" -gt 0 ] || break
    MATCH=$(gh api "repos/${GITHUB_REPOSITORY}/issues/${n}" \
      --jq "select(.state == \"open\" and .title == \"${TITLE}\"
            and ([.labels[].name] | index(\"${LABEL}\"))) | .number" 2>/dev/null || true)
    if [ -n "$MATCH" ]; then KEEP="$MATCH"; fi
  done

  if [ -n "$KEEP" ]; then
    # Carry this leg's row over before closing, so the failure it recorded
    # stays on the surviving tracker instead of being stranded in the body of
    # a closed duplicate.
    printf '%s\n' "$TABLE" > /tmp/comment.md
    gh issue comment "$KEEP" -F /tmp/comment.md || true
    gh issue close "$MINE" \
      --comment "Duplicate of #${KEEP} (concurrent leg failure); consolidating outage tracking there." \
      2>/dev/null || true
  fi
fi
