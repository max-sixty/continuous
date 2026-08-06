#!/usr/bin/env bash
# Mark the GitHub notification thread for the triggering event as read so the
# scheduled tend-notifications poll doesn't burn tokens rediscovering it. The
# thread is only marked when its updated_at predates this run's start — newer
# activity stays unread for the next workflow run to handle. Shared verbatim by
# both harness actions; the caller gates it on a successful run.
#
# Inputs (env): GITHUB_TOKEN (for gh), GITHUB_EVENT_NAME, GITHUB_EVENT_PATH,
# GITHUB_REPOSITORY, GITHUB_RUN_ID (from Actions).
set -eo pipefail

case "$GITHUB_EVENT_NAME" in
  pull_request_target|pull_request_review|pull_request_review_comment)
    NUM=$(jq -r '.pull_request.number' "$GITHUB_EVENT_PATH")
    SUBJECT_URL="https://api.github.com/repos/${GITHUB_REPOSITORY}/pulls/${NUM}"
    ;;
  issue_comment)
    # issue_comment fires for both issues AND PR conversation comments,
    # but PR notifications always use /pulls/N in subject.url. Detect
    # via the issue's pull_request field.
    NUM=$(jq -r '.issue.number' "$GITHUB_EVENT_PATH")
    if [ "$(jq -r '.issue.pull_request.url // empty' "$GITHUB_EVENT_PATH")" != "" ]; then
      SUBJECT_URL="https://api.github.com/repos/${GITHUB_REPOSITORY}/pulls/${NUM}"
    else
      SUBJECT_URL="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/${NUM}"
    fi
    ;;
  issues)
    NUM=$(jq -r '.issue.number' "$GITHUB_EVENT_PATH")
    SUBJECT_URL="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/${NUM}"
    ;;
  *)
    exit 0
    ;;
esac

# Same tolerance as the fetch below: the agent run already succeeded, so a
# transient API error here must not fail the composite action. Without the
# timestamp the `updated_at <= started` guard can't be evaluated, and marking
# unconditionally would swallow activity that arrived mid-run — so skip this
# cycle and leave the thread unread for the scheduled poll to pick up.
if ! RUN_STARTED_AT=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}" \
  --jq '.run_started_at') || [ -z "$RUN_STARTED_AT" ]; then
  echo "::warning::Could not read run_started_at; leaving notification unread (non-fatal)"
  exit 0
fi

# Wrap in a subshell — a transient API error (non-JSON response)
# must not fail the composite action.
(gh api notifications \
  | jq -r --arg url "$SUBJECT_URL" --arg started "$RUN_STARTED_AT" \
      '.[] | select(.subject.url == $url and .updated_at <= $started) | .id' \
  | while read -r tid; do
      [ -n "$tid" ] || continue
      gh api "notifications/threads/$tid" -X PATCH || true
    done) || echo "::warning::Failed to mark notification as read (non-fatal)"
