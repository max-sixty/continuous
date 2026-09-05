#!/usr/bin/env bash
# Check the live PR before posting a review.
#
# Usage: review-preflight.sh <pr-number> [--edit-review <id>] [-- command ...]
#
# Prints `post: <reason>` when the review may proceed or `skip: <reason>` when
# it must stop. A re-targeted post also prints `delta: <path>`; that file holds
# the commits pushed since the review began, excluding base-branch churn. A
# non-zero exit means no decision was made. With a command, the preflight runs
# it only against an unchanged, open head; this is the final posting boundary.
#
# env: GITHUB_REPOSITORY (optional; defaults to the checkout's remote)
#      GITHUB_EVENT_PATH (optional; ready_for_review replaces a draft review)
#      REVIEWED_HEAD_FILE (test-only override of /tmp/reviewed-head)

set -euo pipefail

PR="$1"
shift
EDIT_REVIEW_ID=""
if [ "${1:-}" = "--edit-review" ]; then
  if [ "$#" -lt 2 ]; then
    echo "review-preflight: --edit-review needs a numeric review id" >&2
    exit 1
  fi
  EDIT_REVIEW_ID="${2:-}"
  shift 2
  if [[ ! "$EDIT_REVIEW_ID" =~ ^[0-9]+$ ]]; then
    echo "review-preflight: --edit-review needs a numeric review id" >&2
    exit 1
  fi
fi
COMMAND=()
if [ "${1:-}" = "--" ]; then
  shift
  COMMAND=("$@")
  if [ "${#COMMAND[@]}" -eq 0 ]; then
    echo "review-preflight: -- needs a command" >&2
    exit 1
  fi
elif [ "$#" -ne 0 ]; then
  echo "review-preflight: unexpected arguments: $*" >&2
  exit 1
fi
REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
PIN_FILE="${REVIEWED_HEAD_FILE:-/tmp/reviewed-head}"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DELTA_FILE=""

skip() {
  printf 'skip: %s\n' "$1"
  exit 0
}

post() {
  printf 'post: %s\n' "$1"
  if [ -n "$DELTA_FILE" ]; then
    printf 'delta: %s\n' "$DELTA_FILE"
  fi
  if [ "$RETARGETED" = true ]; then
    echo "$REVIEWED" > "$PIN_FILE"
  fi
}

# Every posting recipe uses this value as commit_id. An empty value would let
# GitHub anchor the review at code this session did not read.
REVIEWED=$(cat "$PIN_FILE" 2>/dev/null || true)
if [[ ! "$REVIEWED" =~ ^[0-9a-f]{40}$ ]]; then
  echo "review-preflight: $PIN_FILE does not hold a commit sha" >&2
  exit 1
fi

PR_VIEW=$(gh pr view "$PR" --repo "$REPO" \
  --json headRefOid,state,baseRefOid \
  --jq '"\(.headRefOid) \(.state) \(.baseRefOid)"')
read -r CURRENT_HEAD PR_STATE BASE_SHA <<<"$PR_VIEW"
if [ -z "$CURRENT_HEAD" ] || [ -z "$PR_STATE" ] || [ -z "$BASE_SHA" ]; then
  echo "review-preflight: could not read PR #$PR" >&2
  exit 1
fi

if [ "$PR_STATE" != "OPEN" ]; then
  skip "PR is $PR_STATE"
fi

RETARGETED=false
if [ "$CURRENT_HEAD" != "$REVIEWED" ]; then
  if [ "${#COMMAND[@]}" -ne 0 ]; then
    skip "HEAD moved to $CURRENT_HEAD before the outward action"
  fi
  git fetch --no-tags --quiet origin "refs/pull/$PR/head" || true
  git fetch --no-tags --quiet origin "$BASE_SHA" || true

  if git merge-base --is-ancestor "$REVIEWED" "$CURRENT_HEAD"; then
    :
  else
    STATUS=$?
    if [ "$STATUS" -eq 1 ]; then
      skip "cannot re-target onto $CURRENT_HEAD — $REVIEWED is no longer an ancestor; leaving it to the queued review"
    fi
    echo "review-preflight: git merge-base failed with status $STATUS" >&2
    exit "$STATUS"
  fi

  DELTA_FILE=$(mktemp)
  # Run outside a command substitution so `set -e` catches either log failing.
  # The first log excludes base churn; the second identifies base merges whose
  # conflict resolutions still need inspection with `git show --cc`.
  {
    git log -p --no-merges --format='%h %s' "$REVIEWED..$CURRENT_HEAD" --not "$BASE_SHA"
    git log --format='base merge: %h %s' --merges "$REVIEWED..$CURRENT_HEAD"
  } > "$DELTA_FILE"

  REVIEWED="$CURRENT_HEAD"
  RETARGETED=true
fi

FORCE=false
if [ -r "${GITHUB_EVENT_PATH:-}" ] \
  && jq -e '.action == "ready_for_review"' "$GITHUB_EVENT_PATH" >/dev/null 2>&1; then
  FORCE=true
fi

REVIEW_STATE=$(GITHUB_REPOSITORY="$REPO" "$HERE/bot-review-state.sh" "$PR")
STATE_HEAD=$(jq -r '.head_sha // empty' <<<"$REVIEW_STATE")
if [ "$STATE_HEAD" != "$REVIEWED" ]; then
  skip "HEAD moved to ${STATE_HEAD:-an unreadable value} during the preflight"
fi

ALREADY=$(jq -r --argjson force "$FORCE" --arg edit "$EDIT_REVIEW_ID" '
      if $edit != "" then
        if .at_head != null
           and (.at_head.id | tostring) == $edit
           and (.orphan_id | tostring) == $edit
        then ""
        else "cannot edit requested orphan review \($edit)" end
      elif .at_head == null or ($force and .at_head.draft_mode) then ""
      else "already carries a \(.at_head.state) review \(.at_head.id)" end' <<<"$REVIEW_STATE")

if [ -n "$ALREADY" ]; then
  skip "$REVIEWED $ALREADY"
fi

FINAL_VIEW=$(gh pr view "$PR" --repo "$REPO" \
  --json headRefOid,state --jq '"\(.headRefOid) \(.state)"')
read -r FINAL_HEAD FINAL_STATE <<<"$FINAL_VIEW"
if [ -z "$FINAL_HEAD" ] || [ -z "$FINAL_STATE" ]; then
  echo "review-preflight: could not re-read PR #$PR" >&2
  exit 1
fi
if [ "$FINAL_STATE" != "OPEN" ]; then
  skip "PR is $FINAL_STATE"
fi
if [ "$FINAL_HEAD" != "$REVIEWED" ]; then
  skip "HEAD moved to $FINAL_HEAD during the preflight"
fi

if [ "$RETARGETED" = true ]; then
  post "re-targeted onto $REVIEWED — read the delta before posting"
else
  post "$REVIEWED is still the head you reviewed"
  if [ "${#COMMAND[@]}" -ne 0 ]; then
    "${COMMAND[@]}"
  fi
fi
