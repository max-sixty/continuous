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

# One row per incident, in the same table format whether it seeds an issue
# body (first one) or is appended as a comment (every later one), so both
# render identically. Stamps the time when called — capture it once per run.
run_issue_row() {
  local ref
  ref=$(run_issue_ref)
  printf '%s\n%s\n%s' \
    "| When | Run | Trigger |" \
    "|------|-----|---------|" \
    "| $(date -u +%Y-%m-%dT%H:%M:%SZ) | [workflow run](${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}) | ${ref:-N/A} |"
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
# both, and every caller acts on the difference: filing a fresh record because
# the read failed is how a repo ends up with two open ones, and the reconcile
# that would close the loser reads the same list that just failed. `set -e`
# does not reach inside the command substitutions the callers read this through,
# so the status has to be carried out explicitly to be seen at all.
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

# Create from a body on stdin, reconcile the duplicates the create-create race
# let through, and print the surviving issue number. Returns non-zero, having
# printed nothing, when the create itself fails.
#
# Callers sleep a jittered interval before their check-then-act, which narrows
# the window when a matrix workflow's legs trip at near-identical times but
# cannot close it: two legs can still both read the list as empty within the
# few seconds the index takes to reflect a fresh create, and each files its
# own. So settle for the index, list every open issue on the label, keep the
# lowest-numbered, and close the rest. Idempotent and convergent — every
# racing leg computes the same keeper, and closing an already-closed
# duplicate is a no-op.
#
# Everything but the number goes to stderr, so the log keeps the created URL
# while the caller can read the keeper straight out of stdout.
run_issue_create_and_reconcile() {
  local label=$1 title=$2
  # Carry a failed create out as this function's status. `set -e` does not
  # reach inside a command substitution unless `inherit_errexit` is set, and
  # every caller reads the number back through one — so without this the
  # failure runs on to the `printf` at the end, whose success becomes the
  # substitution's status, and the caller is handed an empty number it takes
  # for a filed issue.
  gh issue create --title "$title" --label "$label" -F - >&2 || return 1

  sleep 5
  local open keep
  # The same predicate the lookup uses, because it is the same call. Selecting
  # on the label alone would let an unrelated issue carrying it outrank this
  # one on lowest-number, and the record just filed would be closed as that
  # issue's duplicate.
  #
  # A failed read here is not fatal: the issue is filed either way, and the
  # contract already allows handing back no number. Tested rather than left
  # bare, because a bare assignment *does* propagate its status — unlike the
  # substitution-wrapped calls above — so `set -e` would take the caller down
  # in the gap between the create and the caller's own reporting of it.
  if ! open=$(run_issue_matching "$label" open "$title"); then
    echo "Could not list ${label} issues to reconcile duplicates; leaving any in place." >&2
    return 0
  fi
  keep=${open%%$'\n'*}
  echo "$open" | tail -n +2 | while read -r dup; do
    [ -z "$dup" ] && continue
    gh issue close "$dup" \
      --comment "Duplicate of #${keep} (concurrent run); consolidating tracking there." \
      >&2 2>/dev/null || true
  done
  printf '%s' "$keep"
}
