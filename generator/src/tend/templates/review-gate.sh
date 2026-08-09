# shellcheck shell=bash
# Pre-check for tend-review: decide whether the agent needs to boot at all.
#
# The review job runs without cancel-in-progress, so a push mid-review queues
# a replacement run while the live session keeps going, folds the push in, and
# stamps each commit it examined with a `tend-review/<pr>` commit status (review
# skill, "Stamp examined HEADs"). The concurrency group holds this run until
# that session ends; by then the live HEAD is usually stamped and there is
# nothing left to do. Judged against the live PR, not the event payload — a
# queued run's payload is stale by construction.
#
# Inlined into the generated workflow (adopter repos have no copy of this
# file), so it stays self-contained: env in, GITHUB_OUTPUT out. Any write-scoped
# actor could forge the stamp to suppress a review; that actor can already
# cancel the run itself, so the merge gate — not this check — remains the
# security boundary.
#
# env: PR, EVENT_ACTION, GITHUB_REPOSITORY, GITHUB_OUTPUT, GITHUB_TOKEN

# Only `synchronize` can be a stale duplicate of an examination that already
# happened: `opened` has no prior run, and `reopened` / `ready_for_review`
# ask for a fresh pass even on a stamped commit.
if [ "$EVENT_ACTION" != "synchronize" ]; then
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Fail open on API errors: a redundant agent run beats a silently skipped
# review. The parse belongs inside the guard — GitHub sometimes returns an
# HTML error page with a 200 during a blip, so a zero `gh` exit doesn't mean
# the body is JSON, and an unguarded `jq` under the run block's `bash -e`
# would fail the step (fail-closed) instead.
if ! PR_INFO=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PR" 2>/dev/null) \
  || ! STATE=$(echo "$PR_INFO" | jq -re '.state'); then
  echo "PR #$PR fetch failed — proceeding without the pre-check"
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi
if [ "$STATE" != "open" ]; then
  echo "PR #$PR is $STATE — skipping"
  echo "should_run=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

# The stamp context carries the PR number: one branch can be two open PRs
# (same head, different base), and each base means a different diff, so an
# examination of one must not gate the other.
HEAD=$(echo "$PR_INFO" | jq -r '.head.sha')
STAMPED=$(gh api "repos/$GITHUB_REPOSITORY/commits/$HEAD/status?per_page=100" 2>/dev/null \
  | jq --arg ctx "tend-review/$PR" \
    '[.statuses[]? | select(.context == $ctx and .state == "success")] | length' \
  || echo 0)
if [ "${STAMPED:-0}" -gt 0 ]; then
  echo "HEAD $HEAD already examined (tend-review/$PR) — skipping"
  echo "should_run=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

echo "should_run=true" >> "$GITHUB_OUTPUT"
