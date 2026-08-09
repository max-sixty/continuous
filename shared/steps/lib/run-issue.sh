#!/usr/bin/env bash
# Shared machinery for the issues the actions file about their own runs:
# `tend-outage` (report-failure.sh) and `tend-rate-limit`
# (rate-limit-preflight.sh). Both name the run and the trigger it stranded in
# one table format, and both race with sibling jobs failing or tripping at the
# same instant.
#
# Sourced, not executed. Callers keep their own policy about a closed issue,
# because the two differ and the difference is deliberate: an outage issue
# closed as resolved must not swallow the next incident, so report-failure
# files a fresh one; the rate-limit issue is a single long-lived record whose
# closes *are* the approvals, so the preflight reopens it.
#
# Inputs (env, from Actions): GITHUB_SERVER_URL, GITHUB_REPOSITORY,
# GITHUB_RUN_ID, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH.

# A one-line reference to the triggering context, for the Trigger column.
# This is the only pointer back to the work a refused or failed run stranded,
# so every trigger that carries one names it; `// empty` keeps a missing field
# out of the cell as blank rather than the literal `null` jq would print, and
# the caller's `${REF:-N/A}` turns that into N/A. Empty for events with no
# thread of their own (schedule, workflow_dispatch).
run_issue_ref() {
  local num id
  case "$GITHUB_EVENT_NAME" in
    pull_request_target | pull_request_review | pull_request_review_comment)
      num=$(jq -r '.pull_request.number // empty' "$GITHUB_EVENT_PATH")
      ;;
    issues | issue_comment)
      num=$(jq -r '.issue.number // empty' "$GITHUB_EVENT_PATH")
      ;;
    repository_dispatch)
      # tend-mention relays review events through a secretless job that
      # re-posts them as a repository_dispatch, so the PR number arrives in
      # the payload rather than in a `pull_request` object.
      num=$(jq -r '.client_payload.pr // empty' "$GITHUB_EVENT_PATH")
      ;;
    workflow_run)
      # Link the run being fixed — without its id there is no way back to the
      # failure the ci-fix job was dispatched to handle.
      id=$(jq -r '.workflow_run.id // empty' "$GITHUB_EVENT_PATH")
      if [ -n "$id" ]; then
        printf 'CI fix for [run %s](%s/%s/actions/runs/%s)' \
          "$id" "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY" "$id"
      else
        printf 'CI fix for workflow run'
      fi
      return
      ;;
  esac
  printf '%s' "${num:+#${num}}"
}

# The Run cell's link to this run, as it appears in a row. One definition
# because two things have to agree on it exactly: the row `run_issue_row`
# writes, and the dedup that recognises a row already recorded for this run.
# Whole anchor rather than the bare URL, so a human comment merely mentioning
# the run can't be mistaken for a generated row, and a longer run id carrying
# this one as a prefix can't match it.
run_issue_anchor() {
  printf '[workflow run](%s/%s/actions/runs/%s)' \
    "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY" "$GITHUB_RUN_ID"
}

# One row per incident, in the same table format whether it seeds an issue
# body (first one) or is appended as a comment (every later one), so both
# render identically. Stamps the time when called — capture it once per run.
run_issue_row() {
  local ref
  ref=$(run_issue_ref)
  printf '%s\n%s\n%s' \
    "| When | Run | Trigger |" \
    "|------|-----|---------|" \
    "| $(date -u +%Y-%m-%dT%H:%M:%SZ) | $(run_issue_anchor) | ${ref:-N/A} |"
}

# Every issue this bot filed under one title carrying one label, lowest number
# first, one per line. The single definition of which issues are this record's,
# so the lookup and the reconciler cannot come to disagree about it.
#
# All three constraints earn their place. The label alone pins neither author
# nor title, and the bot holds `issues: write` — so a label put on somebody
# else's issue would otherwise nominate it, which matters most where a close on
# that issue is read as an approval. Ordered by number, rather than `gh issue
# list`'s newest-first, because a race can leave a duplicate that the reconcile
# then closes; the lowest is the one every caller must agree on. A label no
# repo has yet returns an empty list rather than an error.
#
# Titles are fixed constants in the callers, so interpolating one into the jq
# filter introduces no quoting hazard.
run_issue_matching() {
  local label=$1 state=$2 title=$3
  gh issue list --label "$label" --state "$state" --author @me --limit 100 \
    --json number,title \
    --jq "map(select(.title == \"${title}\")) | sort_by(.number) | .[].number"
}

# The one that counts: lowest-numbered, empty when there is none. Sliced in the
# shell rather than piped to `head`, which would close the pipe under `pipefail`
# and can take the whole script down with it.
#
# Returns non-zero, having printed nothing, when the list read itself fails.
# That is a different fact from "there is none", though an empty string states
# both, and a caller that conflates them files a fresh record while one is
# already open. The reconcile below will not catch that duplicate: it only
# probes the ten numbers under the one it just created, and an existing record
# is usually far older than that. `set -e` does not reach inside the command
# substitutions the callers read this through, so the status has to be carried
# out explicitly to be seen at all.
run_issue_canonical() {
  local matching
  matching=$(run_issue_matching "$1" "$2" "$3") || return 1
  printf '%s' "${matching%%$'\n'*}"
}

# Creating the label is best-effort: it already exists on every repo after the
# first incident, and `gh label create` has no idempotent form.
run_issue_ensure_label() {
  local label=$1 description=$2 color=$3
  gh label create "$label" --description "$description" --color "$color" 2>/dev/null || true
}

# Create from a body on stdin, reconcile the duplicate the create-create race
# let through, and print the surviving issue number. Returns non-zero, having
# printed nothing, when the create itself fails. The third argument is this
# run's row: when this leg stands down it carries that row onto the keeper
# first, so an incident it recorded is not stranded in the body of a closed
# duplicate. Required, not optional — a caller that omits it strands the very
# record this function exists to preserve, so it aborts rather than creating an
# issue it would then close without carrying anything over.
#
# Callers sleep a jittered interval before their check-then-act, which narrows
# the window when sibling jobs trip at near-identical times but cannot close
# it: two legs can still both read the list as empty within the few seconds
# the index takes to reflect a fresh create, and each files its own.
#
# Reconcile by probing issue numbers directly rather than re-listing. A
# settle-then-list reconcile reads the same lagging index that lost the race in
# the first place: observed in practice, a sibling created 3s earlier was still
# absent from the list while one created 6s earlier was present, so two legs
# whose creates landed in the same second each read back only their own issue
# and closed nothing. `GET /issues/{n}` is a primary-key read and returns a
# sibling the instant it exists, so no settle is needed.
#
# Issue numbers are monotonic, so any racing sibling sits just below ours: scan
# a short window downwards and defer to the lowest match. Convergent — every
# leg computes the same keeper from its own vantage point, and only
# higher-numbered legs stand down.
#
# Self-close-only is a deliberate narrowing: the previous reconcile closed
# every higher duplicate it could see, so a leg that dies between its create
# and its probe (cancelled job, or every probe erroring — indistinguishable
# from "no sibling" here) now leaves a record no other leg will close. That
# only ever worked when the lagging list happened to be current, which is the
# very thing that failed in production, and scanning upwards instead cannot
# fix it: higher numbers may not exist yet at probe time, so it would not
# converge. The residual window is strictly narrower than the one it replaces,
# and it fails toward an extra open record rather than scattered rows.
#
# Everything but the number goes to stderr, so the log keeps the created URL
# while the caller can read the keeper straight out of stdout.
run_issue_create_and_reconcile() {
  # The row is required, and the message must stay apostrophe-free: the word of
  # a `${x:?word}` is parsed for quotes, so a lone `'` opens a string that runs
  # to EOF and breaks the file rather than this call.
  local label=$1 title=$2 row=${3:?the row for this run is required}
  local url mine
  # Carry a failed create out as this function's status, explicitly. `set -e`
  # does not reach inside a command substitution unless `inherit_errexit` is
  # set, and every caller reads the number back through one — so without the
  # `|| return 1` the failure would run on to the `printf` at the end, whose
  # success becomes the substitution's status, and the caller is handed an
  # empty number it takes for a filed issue. The create keeps its own
  # assignment rather than being wrapped in another command (`basename "$(gh
  # issue create ...)"`), which would report the wrapper's status instead.
  url=$(gh issue create --title "$title" --label "$label" -F -) || return 1
  echo "$url" >&2
  mine=${url##*/}

  # The same three constraints run_issue_matching applies — author, title,
  # label — checked one issue at a time instead of over a list. Author above
  # all: the bot holds `issues: write`, so without it a label put on somebody
  # else's issue could be adopted as the keeper, and on the rate-limit record a
  # close is read as an approval. Failing to read our own identity leaves the
  # pair open rather than deferring to an issue we cannot vouch for.
  local me
  me=$(gh api user --jq '.login' 2>/dev/null || true)

  local keep="" n match
  if [ -n "$me" ]; then
    for n in $(seq $((mine - 1)) -1 $((mine - 10))); do
      [ "$n" -gt 0 ] || break
      match=$(gh api "repos/${GITHUB_REPOSITORY}/issues/${n}" \
        --jq "select(.state == \"open\" and .title == \"${title}\"
              and .user.login == \"${me}\"
              and ([.labels[].name] | index(\"${label}\"))) | .number" 2>/dev/null || true)
      if [ -n "$match" ]; then keep="$match"; fi
    done
  fi

  if [ -z "$keep" ]; then
    printf '%s' "$mine"
    return
  fi

  # Carry the row over only when the keeper does not already cite this run.
  # Matrix legs share one GITHUB_RUN_ID, so when the racing legs belong to the
  # same matrix the keeper's seed row already points at this run and a carried
  # row would just duplicate it, differing only in its timestamp. The
  # cross-workflow race has distinct run ids, so it still carries over.
  # Anchor on the generated row's run link rather than the bare id, so a human
  # comment mentioning the run cannot suppress the row. Read into a variable
  # rather than piped to grep, which would close the pipe under `pipefail` and
  # could report a match as a read failure.
  local seen run_url
  run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
  seen=$(gh issue view "$keep" --json body,comments \
    --jq '.body + "\n" + ([.comments[].body] | join("\n"))' 2>/dev/null || true)
  if ! grep -qF "[workflow run](${run_url})" <<< "$seen"; then
    printf '%s\n' "$row" | gh issue comment "$keep" -F - >&2 || true
  fi

  gh issue close "$mine" \
    --comment "Duplicate of #${keep} (concurrent run); consolidating tracking there." \
    >&2 2>/dev/null || true
  printf '%s' "$keep"
}
