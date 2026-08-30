# shellcheck shell=bash
# Pre-check for tend-review: decide whether the agent needs to boot at all.
#
# Review runs serialize per PR without cancel-in-progress, so a push mid-review
# queues a replacement run while the live session keeps going, folds the push
# into what it already read, and posts its review anchored at the live head
# (review skill, "Posting mechanics"). The concurrency group holds this run
# until that session ends; by then the head it was queued for usually already
# carries that review and there is nothing left to do. Judged against the live
# PR, not the event payload — a queued run's payload is stale by construction.
#
# The only state read is the bot's own published reviews. Nothing stamps a
# marker, so a session killed by a timeout or a cancellation leaves no anchor
# and this run does the review that was never posted — the queued run stays the
# recovery path.
#
# That is the whole difference from the gate #1078 removed, and why this is a
# re-add rather than a reversal. That one read a per-PR commit status the
# workflow wrote itself, which put a visible check row on every reviewed commit
# and, being a status, was writable by anything holding a token on the repo.
# Its context name is deliberately not spelled out here: this text is inlined
# into the workflow, where a test asserts that name is gone for good.
#
# The skip was never what was wrong with it; the signal was. A review is a
# signal the bot has to publish anyway, only the bot can write it, and it
# renders as the review it already is.
#
# What is skipped is the agent, not the answer: a question put to the bot
# reaches it through tend-mention, and the notifications poll is the backstop.
# Re-running a skipped run skips again — the head is still covered — so a
# deliberate re-review comes from a mention, not from the Actions UI.
#
# Inlined into the generated workflow (adopter repos have no copy of this
# file), so it stays self-contained: env in, GITHUB_OUTPUT out.
#
# env: PR, EVENT_ACTION, BOT_NAME, GITHUB_REPOSITORY, GITHUB_OUTPUT, GITHUB_TOKEN

# Two actions can arrive on a head a review already covers. `synchronize` is
# the common one. `reopened` is the same shape: the session's own step 1 stops
# on an already-reviewed head, so admitting it buys a fully billed no-op.
#
# The rest ask for a pass whatever the head carries — `opened` has no prior run
# at all, and `ready_for_review` sets the session's `FORCE_FULL_REVIEW`, which
# bypasses the already-reviewed check so a draft's COMMENT-only pass is
# redone in full. An action nothing here names (an override widening `types:`)
# falls through to a run, which is the fail-open direction.
case "$EVENT_ACTION" in
  synchronize | reopened) ;;
  *)
    echo "should_run=true" >> "$GITHUB_OUTPUT"
    exit 0
    ;;
esac

# Fail open on API errors: a redundant agent run beats a silently skipped
# review. The parse belongs inside the guard — GitHub sometimes returns an
# HTML error page with a 200 during a blip, so a zero `gh` exit doesn't mean
# the body is JSON, and an unguarded `jq` under the run block's `bash -e`
# would fail the step (fail-closed) instead.
if ! PR_INFO=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PR" 2>/dev/null) \
  || ! STATE=$(echo "$PR_INFO" | jq -re '.state') \
  || ! HEAD=$(echo "$PR_INFO" | jq -re '.head.sha'); then
  echo "PR #$PR fetch failed — proceeding without the pre-check"
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi
if [ "$STATE" != "open" ]; then
  echo "PR #$PR is $STATE — skipping"
  echo "should_run=false" >> "$GITHUB_OUTPUT"
  exit 0
fi
# A review anchors this head when it is submitted (the endpoint also returns
# the caller's own unsubmitted PENDING reviews, whose null `submitted_at` would
# read as newer than any force-push below) and it has a body, or is an approval
# (whose body is empty by convention). One carrying only inline comments is
# missed — they hang off a separate endpoint — and a zero-body COMMENTED review
# is GitHub's synthetic container for an inline reply, which reviewed nothing;
# both fail open, into a run.
#
# A rewrite re-points earlier reviews' `commit_id` at the NEW head, so the
# anchor alone cannot tell a review of this commit from a review of code that
# was rewritten away (see `bot-review-state.sh`, which the session reads the
# same rule from). Discount anything submitted before the newest force-push,
# and this run reviews the rewritten head in full.
if ! ANCHORS=$(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$PR/reviews?per_page=100" \
    --jq ".[] | select(.user.login == \"$BOT_NAME\" and .commit_id == \"$HEAD\"
          and .submitted_at != null
          and ((.body | length) > 0 or .state == \"APPROVED\")) | .submitted_at" 2>/dev/null) \
  || ! FORCE_PUSHES=$(gh api --paginate "repos/$GITHUB_REPOSITORY/issues/$PR/timeline?per_page=100" \
    --jq '.[] | select(.event == "head_ref_force_pushed") | .created_at' 2>/dev/null); then
  echo "PR #$PR review history fetch failed — proceeding without the pre-check"
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

# `--jq` runs once per page, so reduce out here. GitHub's timestamps are
# ISO-8601 in UTC, which sorts lexically. The empty string both reductions
# yield on no rows is what makes the one comparison enough: no force-push
# leaves a value every anchor beats, and no anchor leaves one that beats
# nothing — so a missing anchor falls through to a run without its own test.
LAST_ANCHOR=$(printf '%s\n' "$ANCHORS" | sort | tail -n1)
LAST_FORCE_PUSH=$(printf '%s\n' "$FORCE_PUSHES" | sort | tail -n1)
if [[ "$LAST_ANCHOR" > "$LAST_FORCE_PUSH" ]]; then
  echo "HEAD $HEAD already reviewed at $LAST_ANCHOR — skipping"
  echo "should_run=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

echo "should_run=true" >> "$GITHUB_OUTPUT"
