#!/usr/bin/env bash
# Resolves which of the bot's reviews actually anchors the PR's current head,
# and emits the answer as one JSON object.
#
# Usage: bot-review-state.sh <pr-number>
#
# Two GitHub behaviours make `.commit_id` alone unreadable, and every caller
# that reads a review anchor has to account for both:
#
#   Reply containers. Replying to a review thread (POST
#   /pulls/{n}/comments/{id}/replies) makes GitHub wrap the reply in a synthetic
#   zero-body COMMENTED review anchored at the then-current head. The newest
#   review record therefore routinely points at a commit nothing reviewed. A
#   review counts as *substantive* when it has a body, owns a top-level
#   (non-reply) inline comment, or is an APPROVED — containers are always
#   zero-body COMMENTED, while an empty-body approval and an empty-body review
#   carrying real inline findings both still anchor.
#
#   Force-push re-anchoring. A rewrite does not just move the head: GitHub
#   re-points earlier reviews' `.commit_id` at the NEW head. So a review of code
#   that no longer exists reports the current head, and `.commit_id` alone
#   cannot tell an ordinary push from a rewrite. Anything submitted before the
#   newest `head_ref_force_pushed` is discounted.
#
# Output (one object, all fields always present; absent values are null or ""):
#   head_sha            the PR's current head
#   last_force_push_at  newest head_ref_force_pushed timestamp, "" if none
#   last_substantive    {id, sha, state, at} — newest substantive bot review at
#                       any anchor, null if none. `sha` is unreliable when
#                       force_pushed_since is true; that is what the flag is for.
#   force_pushed_since  true when a rewrite postdates last_substantive — the
#                       commit the bot read was rewritten away, so its anchor
#                       now names the current head and every incremental keyed
#                       on it under-reports
#   at_head             {id, state, at} — newest substantive bot review anchored
#                       at head and submitted after the newest rewrite, else
#                       null. Non-null means this head is genuinely reviewed.
#   orphan_id           id of the newest body-bearing bot review anchored at
#                       head post-rewrite, else null. A partially-failed review
#                       POST persists the body and drops the inline comments;
#                       this is the record to edit rather than duplicate.
#   fresh_approval_sha  commit_id of the newest post-rewrite bot APPROVED, ""
#                       if none — an approval that was genuinely earned
#   stale_approval_id   id of the newest bot APPROVED when that newest approval
#                       predates the rewrite, "" otherwise. Newest-then-test,
#                       never test-then-newest: the question is whether the
#                       approval now setting the PR's state is stale, so
#                       filtering first would name a superseded one and leave
#                       the live approval standing.
#
# env: GITHUB_REPOSITORY (optional; falls back to the checkout's remote)

set -euo pipefail

PR="$1"
REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
BOT=$(gh api user --jq '.login')

HEAD_SHA=$(gh pr view "$PR" --repo "$REPO" --json commits --jq '.commits[-1].oid')

# Review ids owning at least one top-level inline comment. `gh api --paginate`
# applies `--jq` per page, so reduce with a second jq over the merged stream
# rather than inside the filter.
SUBSTANTIVE=$(gh api --paginate "repos/$REPO/pulls/$PR/comments" \
  --jq '.[] | select(.in_reply_to_id == null) | .pull_request_review_id' | jq -s 'unique')

LAST_FORCE_PUSH_AT=$(gh api --paginate "repos/$REPO/issues/$PR/timeline" \
  | jq -rs 'add | [.[] | select(.event == "head_ref_force_pushed") | .created_at] | max // ""')

gh api --paginate "repos/$REPO/pulls/$PR/reviews" \
  | jq -s --argjson sub "$SUBSTANTIVE" --arg bot "$BOT" --arg head "$HEAD_SHA" \
      --arg fp "$LAST_FORCE_PUSH_AT" '
    add
    | [.[] | select(.user.login == $bot)] as $mine
    | ($mine | map(select((.body | length) > 0 or (.id | IN($sub[])) or .state == "APPROVED"))) as $subs
    | ($subs | last) as $lastsub
    | ($mine | map(select(.state == "APPROVED")) | last) as $lastapp
    | ($fp == "") as $norewrite
    | {
        head_sha: $head,
        last_force_push_at: $fp,
        last_substantive: (if $lastsub == null then null else
          {id: $lastsub.id, sha: $lastsub.commit_id, state: $lastsub.state, at: $lastsub.submitted_at}
        end),
        force_pushed_since:
          ($lastsub != null and $fp != "" and $fp > $lastsub.submitted_at),
        at_head: ($subs
          | map(select(.commit_id == $head and ($norewrite or .submitted_at > $fp)))
          | last
          | if . == null then null else {id, state, at: .submitted_at} end),
        orphan_id: ($subs
          | map(select(.commit_id == $head and (.body | length) > 0
                       and ($norewrite or .submitted_at > $fp)))
          | last | .id),
        fresh_approval_sha: (($mine
          | map(select(.state == "APPROVED" and ($norewrite or .submitted_at > $fp)))
          | last | .commit_id) // ""),
        stale_approval_id: (
          if $lastapp != null and $fp != "" and $lastapp.submitted_at < $fp
          then $lastapp.id else "" end),
      }'
