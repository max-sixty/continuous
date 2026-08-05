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
  # Keep the create in its own assignment so `set -e` still sees its exit
  # status. Wrapping it in another command (`basename "$(gh issue create ...)"`)
  # would make the assignment report the wrapper's status, so a failed create
  # would sail past `set -e` with an empty $MINE and the outage unrecorded.
  MINE_URL=$(gh issue create --title "$TITLE" --label "$LABEL" -F /tmp/body.md)
  MINE=${MINE_URL##*/}
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
  #
  # Self-close-only is a deliberate narrowing: the previous reconcile closed
  # every higher duplicate it could see, so a leg that dies between its create
  # and its probe (cancelled job, or every probe erroring — indistinguishable
  # from "no sibling" here) now leaves a tracker no other leg will close. That
  # only bit when the lagging list happened to be current, which is the very
  # thing that failed in production, and scanning upwards instead can't fix it:
  # higher numbers may not exist yet at probe time, so it wouldn't converge.
  # Nothing else reconciles trackers, so the orphan persists until a human or
  # the nightly sweep closes it — a strictly narrower window than the
  # every-create-races-lose one this replaces.
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
    #
    # Only when the keeper doesn't already cite this run. Matrix legs share one
    # GITHUB_RUN_ID (and so one RUN_URL and REF), so when the racing legs belong
    # to the same matrix the keeper's seed row already points at this run and a
    # carried row would just duplicate it — differing only in TIMESTAMP. The
    # cross-workflow race this reconcile was built for has distinct run ids, so
    # it still carries over. Anchor on the full generated-row text, not the bare
    # URL, so a human comment mentioning the run can't suppress the row.
    printf '%s\n' "$TABLE" > /tmp/comment.md
    if ! gh issue view "$KEEP" --json body,comments \
        --jq '.body + "\n" + ([.comments[].body] | join("\n"))' \
        | grep -qF "[workflow run](${RUN_URL})"; then
      gh issue comment "$KEEP" -F /tmp/comment.md || true
    fi
    gh issue close "$MINE" \
      --comment "Duplicate of #${KEEP} (concurrent leg failure); consolidating outage tracking there." \
      2>/dev/null || true
  fi
fi
