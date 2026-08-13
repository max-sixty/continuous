# shellcheck shell=bash
# Pre-check for tend-mention: decide whether the mention is addressed to the
# bot — by name or by engagement — and so whether the agent boots at all.
#
# Inlined into the generated workflow (adopter repos have no copy of this
# file), so it stays self-contained: env in, GITHUB_OUTPUT out.
#
# env: BOT_NAME, EVENT_NAME, COMMENT_BODY, COMMENT_AUTHOR, COMMENT_AUTHOR_TYPE,
#      ISSUE_BODY, ISSUE_OR_PR_NUMBER, ISSUE_AUTHOR, PR_URL, PAYLOAD_KIND,
#      PAYLOAD_PR, PAYLOAD_ID, GITHUB_REPOSITORY, GITHUB_OUTPUT, GITHUB_TOKEN
# out: should_run, reason, url, ts

# A relayed review event arrives as identifiers only ({kind, pr, id}): resolve
# them against the API before judging anything, so the words weighed below are
# the ones GitHub holds rather than whatever the payload carried. Any
# write-scoped actor can POST a dispatch, so a forged payload faces the same
# checks a real event does — and fetching by PR and id binds the two, so a
# payload pairing a real review with some other PR dies here instead of
# steering the handle job. The ids are spliced into API paths here and into the
# prompt later, so reject anything but digits at this edge.
KIND="$EVENT_NAME"
if [ "$EVENT_NAME" = "repository_dispatch" ]; then
  KIND="$PAYLOAD_KIND"
  if ! [[ "$PAYLOAD_PR" =~ ^[0-9]+$ && "$PAYLOAD_ID" =~ ^[0-9]+$ ]]; then
    echo "malformed dispatch payload — skipping"
    echo "should_run=false" >> "$GITHUB_OUTPUT"; exit 0
  fi
  case "$KIND" in
    pull_request_review)
      if ! REVIEW=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PAYLOAD_PR/reviews/$PAYLOAD_ID"); then
        echo "review $PAYLOAD_ID not found on PR $PAYLOAD_PR — skipping"
        echo "should_run=false" >> "$GITHUB_OUTPUT"; exit 0
      fi
      REVIEW_AUTHOR=$(echo "$REVIEW" | jq -r '.user.login')
      # REST reports the state uppercase (a webhook payload's is lowercase);
      # normalize so the terminal-approval gate below reads one shape.
      REVIEW_STATE=$(echo "$REVIEW" | jq -r '.state | ascii_downcase')
      COMMENT_BODY=$(echo "$REVIEW" | jq -r '.body // ""')
      echo "url=$(echo "$REVIEW" | jq -r '.html_url')" >> "$GITHUB_OUTPUT"
      # A review without `submitted_at` (a PENDING one, which only a forged
      # dispatch can name) would otherwise write the string `null`, which
      # handle's `date -d` rejects — failing the job red where the empty-value
      # guard would have skipped it.
      echo "ts=$(echo "$REVIEW" | jq -r '.submitted_at // empty')" >> "$GITHUB_OUTPUT"
      ;;
    pull_request_review_comment)
      if ! COMMENT=$(gh api "repos/$GITHUB_REPOSITORY/pulls/comments/$PAYLOAD_ID"); then
        echo "comment $PAYLOAD_ID not found — skipping"
        echo "should_run=false" >> "$GITHUB_OUTPUT"; exit 0
      fi
      # Comments fetch by id alone, so bind the PR explicitly.
      if [ "$(echo "$COMMENT" | jq -r '.pull_request_url')" != "https://api.github.com/repos/$GITHUB_REPOSITORY/pulls/$PAYLOAD_PR" ]; then
        echo "comment $PAYLOAD_ID does not belong to PR $PAYLOAD_PR — skipping"
        echo "should_run=false" >> "$GITHUB_OUTPUT"; exit 0
      fi
      COMMENT_AUTHOR=$(echo "$COMMENT" | jq -r '.user.login')
      COMMENT_BODY=$(echo "$COMMENT" | jq -r '.body // ""')
      echo "url=$(echo "$COMMENT" | jq -r '.html_url')" >> "$GITHUB_OUTPUT"
      echo "ts=$(echo "$COMMENT" | jq -r '.updated_at')" >> "$GITHUB_OUTPUT"
      ;;
    *)
      echo "unknown dispatch kind '$KIND' — skipping"
      echo "should_run=false" >> "$GITHUB_OUTPUT"; exit 0
      ;;
  esac
fi

# Mentions always run
if [ "$KIND" = "issues" ]; then
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

# The bot's own comments never summon it (its PAT-based User account is
# invisible to the Bot-type skip below). Placed *before* the mention check,
# since a bot comment can quote a prior @-mention. Comments only: the bot's
# review *submissions* are judged with the review kind below — a review carries
# reviewer-role signal a comment can't.
if { [ "$KIND" = "issue_comment" ] || [ "$KIND" = "pull_request_review_comment" ]; } \
   && [ "$COMMENT_AUTHOR" = "$BOT_NAME" ]; then
  echo "should_run=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

if [ -n "$COMMENT_BODY" ] && printf '%s\n' "$COMMENT_BODY" | grep -qF "@$BOT_NAME"; then
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  echo "reason=mention" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Other bots' undirected comments (deploy notifications, CI status) summon by
# mention only, never by the engagement heuristics below — which would boot a
# no-op session per notification, twice when the source bot edits its comment.
if [ "$KIND" = "issue_comment" ] && [ "$COMMENT_AUTHOR_TYPE" = "Bot" ]; then
  echo "should_run=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

# A review's record includes review.body (checked above) but NOT the bodies of
# the inline comments attached to the review. Fetch them so a first-contact
# @-mention inside an inline comment is detected on PRs where the bot has no
# prior engagement. One object per line, so `--paginate` concatenates pages
# instead of reducing within one. Keep the `{body, in_reply_to_id}`
# construction: `in_reply_to_id` is an *optional* property, absent rather than
# null on a fresh comment, and building the object normalizes absent to null so
# the `== null` select below counts both shapes. A bare `.in_reply_to_id`
# stream would emit nothing for a fresh comment and count every review as
# reply-only.
if [ "$KIND" = "pull_request_review" ]; then
  INLINE=$(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$PAYLOAD_PR/reviews/$PAYLOAD_ID/comments" \
    --jq '.[] | {body, in_reply_to_id}')

  if printf '%s\n' "$INLINE" | jq -r '.body' | grep -qF "@$BOT_NAME"; then
    echo "should_run=true" >> "$GITHUB_OUTPUT"
    echo "reason=mention" >> "$GITHUB_OUTPUT"
    exit 0
  fi

  FRESH_INLINE=$(printf '%s\n' "$INLINE" | jq -s '[.[] | select(.in_reply_to_id == null)] | length')

  # A contentless approval — no body, no inline comments — asks for nothing,
  # whoever submitted it, and the bot cannot merge on its own. Without this it
  # reads as engagement below and boots a session whose only outcome is a
  # silent exit. `approved` and `$INLINE` empty are both load-bearing: a bare
  # COMMENTED review is how GitHub wraps a human's inline reply (not terminal),
  # and an approval whose nits live inline is a request to the PR's author — on
  # a bot-authored PR, a role the bot has to act in.
  if [ "$REVIEW_STATE" = "approved" ] \
     && [ -z "$COMMENT_BODY" ] \
     && [ -z "$INLINE" ]; then
    echo "should_run=false" >> "$GITHUB_OUTPUT"
    exit 0
  fi
fi

# Non-mention: check bot engagement
if [ "$KIND" = "issue_comment" ]; then
  ISSUE_NUMBER="$ISSUE_OR_PR_NUMBER"

  if [ -z "$PR_URL" ]; then
    if [ "$ISSUE_AUTHOR" = "$BOT_NAME" ]; then
      echo "should_run=true" >> "$GITHUB_OUTPUT"; exit 0
    fi
    if printf '%s\n' "$ISSUE_BODY" | grep -qF "@$BOT_NAME"; then
      echo "should_run=true" >> "$GITHUB_OUTPUT"; exit 0
    fi
    # Don't reduce inside jq: `gh api --paginate` applies `--jq` once per page,
    # so `| length` emits one count per page rather than one overall. Past 100
    # comments the variable holds e.g. `100\n7`, a numeric test on it errors
    # with `integer expression expected`, and the failed test falls through to
    # should_run=false — the bot goes quiet on its most-engaged threads.
    # Capture the per-element stream and test it for emptiness — the bare
    # substitution also keeps a failing `gh api` fatal under GHA's default
    # `bash -e`.
    BOT_COMMENTS=$(gh api --paginate "repos/$GITHUB_REPOSITORY/issues/$ISSUE_NUMBER/comments" \
      --jq ".[] | select(.user.login == \"$BOT_NAME\") | .id")
    if [ -n "$BOT_COMMENTS" ]; then
      echo "should_run=true" >> "$GITHUB_OUTPUT"; exit 0
    fi
    echo "should_run=false" >> "$GITHUB_OUTPUT"; exit 0
  fi

  PR_NUMBER="$ISSUE_NUMBER"
else
  PR_NUMBER="$PAYLOAD_PR"
fi

PR_AUTHOR=$(gh pr view "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" --json author --jq '.author.login')

# The bot's own review summons a session in exactly one shape: the reviewer
# role handing work to the author role — fresh content (a body, or an inline
# comment that isn't a reply) on a PR the bot authored. On another author's PR
# the review session already did whatever the review warranted; an empty-body
# reply-only review is the synthetic container GitHub wraps around an inline
# reply — the same comment the self-comment skip above drops on its other event
# path. Either would otherwise read as engagement below (the BOT_REVIEWS
# heuristic counts this very review) and boot a session that exits silently.
# Sits *after* the inline @-mention scan, so an explicit summons the bot quotes
# still wins.
if [ "$KIND" = "pull_request_review" ] && [ "$REVIEW_AUTHOR" = "$BOT_NAME" ]; then
  if [ "$PR_AUTHOR" = "$BOT_NAME" ] \
     && { [ -n "$COMMENT_BODY" ] || [ "$FRESH_INLINE" -gt 0 ]; }; then
    echo "should_run=true" >> "$GITHUB_OUTPUT"
    echo "reason=participation" >> "$GITHUB_OUTPUT"; exit 0
  fi
  echo "should_run=false" >> "$GITHUB_OUTPUT"; exit 0
fi

if [ "$PR_AUTHOR" = "$BOT_NAME" ]; then
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  echo "reason=participation" >> "$GITHUB_OUTPUT"; exit 0
fi

# Captured, not counted — see the note on the issue-comment lookup above.
BOT_REVIEWS=$(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER/reviews" \
  --jq ".[] | select(.user.login == \"$BOT_NAME\") | .id")
if [ -n "$BOT_REVIEWS" ]; then
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  echo "reason=participation" >> "$GITHUB_OUTPUT"; exit 0
fi

BOT_COMMENTS=$(gh api --paginate "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments" \
  --jq ".[] | select(.user.login == \"$BOT_NAME\") | .id")
if [ -n "$BOT_COMMENTS" ]; then
  echo "should_run=true" >> "$GITHUB_OUTPUT"
  echo "reason=participation" >> "$GITHUB_OUTPUT"; exit 0
fi

echo "should_run=false" >> "$GITHUB_OUTPUT"
