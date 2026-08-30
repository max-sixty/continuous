#!/usr/bin/env bash
# Decides whether this session may post its review, and re-targets the review
# when the PR's head moved while it was being composed.
#
# Usage: review-preflight.sh <pr-number>
#
# A `pull_request_target` review routinely runs 5–10 minutes between reading
# the head and posting the verdict, so three things can have changed underneath
# it. Each is checked here rather than in the skill, because each is a
# deterministic read the session would otherwise re-derive from prose:
#
#   The PR closed. HEAD does not move when a PR closes or merges, so nothing
#   else notices. An APPROVE that lands afterwards is timestamped after the
#   close and reads as the bot approving a closed PR.
#
#   The head moved. The review is re-targeted onto the live head — but only
#   when the reviewed commit is still an ancestor of it. A rewrite leaves
#   nothing to build on, and the queued run reviews the new head in full.
#   `delta` is then the session's only record of what was pushed.
#
#   A review already anchors the head. The queued run and this one can both
#   reach the same commit; `bot-review-state.sh` decides which of the bot's
#   reviews genuinely anchors it (see its header for why `.commit_id` alone
#   cannot be read directly).
#
# The pin file is the contract with the rest of the skill: step 1 writes the
# commit it read there, every posting recipe reads it back as the review's
# `commit_id`, and this script rewrites it on a re-target. `FORCE_FULL_REVIEW`
# is derived here rather than passed in — shell state does not survive a tool
# call, so a value step 1 exported is gone by the time a review is posted.
#
# Output (one object, all fields always present):
#   verdict     "post" — go ahead, pinning `head`; "skip" — post nothing
#   reason      one human-readable line; on a skip, why
#   head        the commit to pin, and what the pin file now holds
#   retargeted  true when the head moved and the review was re-pointed at it
#   delta       what was pushed since the reviewed commit: the authored log
#               (scoped off base churn), then the base merges. "" when the
#               head did not move — and legitimately empty on a fast-forward
#               onto the base, which is why `retargeted` is separate
#
# A non-zero exit is a third outcome, not a verdict: nothing was decided and
# stderr says why. It is the loud direction on purpose — every quiet failure
# here ends with a review that was never posted, or one posted unpinned.
#
# env: GITHUB_REPOSITORY (optional; falls back to the checkout's remote)
#      GITHUB_EVENT_PATH (optional; a `ready_for_review` action lets a full
#                         pass replace Tend's earlier draft COMMENT)
#      REVIEWED_HEAD_FILE (the pin file; defaults to the path the skill names)

set -euo pipefail

PR="$1"
REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
PIN_FILE="${REVIEWED_HEAD_FILE:-/tmp/reviewed-head}"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# The delta reaches jq as a file, never an argument: Linux caps one argv
# string at 131072 bytes, and a mid-review push that regenerates a large file
# blows past that — killing the preflight after the pin file has already been
# advanced, which is the one failure that leaves an unreviewed head looking
# reviewed.
DELTA_FILE=$(mktemp)
trap 'rm -f "$DELTA_FILE"' EXIT

emit() {
  jq -n --arg verdict "$1" --arg reason "$2" --arg head "$3" \
    --argjson retargeted "$4" --rawfile delta "$DELTA_FILE" \
    '{verdict: $verdict, reason: $reason, head: $head,
      retargeted: $retargeted, delta: $delta}'
}

# Fail rather than substitute an empty sha: unpinned, GitHub anchors the review
# at whatever is live when the POST lands, and the review then claims code this
# session never saw.
REVIEWED=$(cat "$PIN_FILE" 2>/dev/null || true)
if [[ ! "$REVIEWED" =~ ^[0-9a-f]{40}$ ]]; then
  echo "review-preflight: $PIN_FILE does not hold a commit sha — step 1 records the reviewed commit there" >&2
  exit 1
fi

# Assigned before `read` so a failing `gh` aborts here: read off a process
# substitution succeeds with empty fields, and an empty state is not "OPEN",
# so an API blip would come back as a considered decision to post nothing.
PR_VIEW=$(gh pr view "$PR" --repo "$REPO" \
  --json headRefOid,state --jq '"\(.headRefOid) \(.state)"')
read -r CURRENT_HEAD PR_STATE <<<"$PR_VIEW"
if [ -z "$CURRENT_HEAD" ] || [ -z "$PR_STATE" ]; then
  echo "review-preflight: could not read PR #$PR — got '$PR_VIEW'" >&2
  exit 1
fi

if [ "$PR_STATE" != "OPEN" ]; then
  emit skip "PR is $PR_STATE" "$REVIEWED" false
  exit 0
fi

PINNED="$REVIEWED"
RETARGETED=false

if [ "$CURRENT_HEAD" != "$REVIEWED" ]; then
  # Re-targeting needs the live head to build on the reviewed one; a rewrite
  # (or a head that won't fetch) leaves nothing to re-target onto.
  BASE_SHA=$(gh pr view "$PR" --repo "$REPO" --json baseRefOid --jq '.baseRefOid')
  git fetch --no-tags --quiet origin "refs/pull/$PR/head" || true
  git fetch --no-tags --quiet origin "$BASE_SHA" || true
  if ! git merge-base --is-ancestor "$REVIEWED" "$CURRENT_HEAD" 2>/dev/null; then
    emit skip \
      "cannot re-target onto $CURRENT_HEAD — $REVIEWED is no longer an ancestor of it, so the push rewrote what was reviewed; leaving it to the queued review" \
      "$REVIEWED" false
    exit 0
  fi
  # A brace group, not `$( … )`: `set -e` does not reach inside a command
  # substitution, so a scoped log that failed would vanish and leave a delta
  # showing only the base merges — a push of the author's own code reading as
  # an "Update branch" click.
  {
    # The author's own new code, scoped off base churn as in step 1:
    # `--not "$BASE_SHA"` drops everything a base merge dragged in, which a
    # plain `git diff` between the two heads would present as the author's.
    git log -p --no-merges --format='%h %s' "$REVIEWED..$CURRENT_HEAD" --not "$BASE_SHA"
    # Base merges, which the scoped log above cannot show — it drops the merge
    # commit and every commit the merge brought in. An "Update branch" click
    # prints nothing there while re-scoping every file's hunks.
    git log --format='base merge: %h %s' --merges "$REVIEWED..$CURRENT_HEAD"
  } > "$DELTA_FILE"
  echo "$CURRENT_HEAD" > "$PIN_FILE"
  PINNED="$CURRENT_HEAD"
  RETARGETED=true
fi

# A forced ready-for-review pass may replace Tend's earlier draft COMMENT, but
# no other substantive review — including a full pass that raced this one.
FORCE=false
EVENT_ACTION=""
if [ -r "${GITHUB_EVENT_PATH:-}" ]; then
  EVENT_ACTION=$(jq -r '.action // ""' "$GITHUB_EVENT_PATH" 2>/dev/null || true)
fi
if [ "$EVENT_ACTION" = "ready_for_review" ]; then
  FORCE=true
fi

STATE_JSON=$(GITHUB_REPOSITORY="$REPO" "$HERE/bot-review-state.sh" "$PR")
ALREADY=$(jq -r --argjson force "$FORCE" '
  if .at_head == null or ($force and .at_head.draft_mode) then ""
  else "\(.at_head.state) review \(.at_head.id)" end' \
  <<<"$STATE_JSON")

if [ -n "$ALREADY" ]; then
  emit skip "$PINNED already carries a $ALREADY" "$PINNED" "$RETARGETED"
  exit 0
fi

if [ "$RETARGETED" = true ]; then
  emit post "re-targeted onto $PINNED — read the delta before posting" "$PINNED" true
else
  emit post "$PINNED is still the head you reviewed" "$PINNED" false
fi
