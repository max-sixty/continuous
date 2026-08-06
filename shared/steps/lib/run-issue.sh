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
      printf '%s' "${num:+#${num}}"
      ;;
    issues | issue_comment)
      num=$(jq -r '.issue.number // empty' "$GITHUB_EVENT_PATH")
      printf '%s' "${num:+#${num}}"
      ;;
    repository_dispatch)
      # tend-mention relays review events through a secretless job that
      # re-posts them as a repository_dispatch, so the PR number arrives in
      # the payload rather than in a `pull_request` object.
      num=$(jq -r '.client_payload.pr // empty' "$GITHUB_EVENT_PATH")
      printf '%s' "${num:+#${num}}"
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
      ;;
  esac
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

# The canonical issue on a label: the lowest-numbered, which is the same rule
# `run_issue_create_and_reconcile` keeps. `gh issue list` orders newest-first,
# so taking `.[0]` off an unsorted list can return a duplicate a race created
# and the reconcile then closed — reopening and appending to a record that was
# already consolidated away. Sorting by number makes every caller agree with
# the reconciler about which issue is the real one.
run_issue_canonical() {
  local label=$1 state=$2
  gh issue list --label "$label" --state "$state" --limit 100 \
    --json number --jq 'sort_by(.number) | .[0].number // empty' 2>/dev/null || echo ""
}

# Creating the label is best-effort: it already exists on every repo after the
# first incident, and `gh label create` has no idempotent form.
run_issue_ensure_label() {
  local label=$1 description=$2 color=$3
  gh label create "$label" --description "$description" --color "$color" 2>/dev/null || true
}

# Create, then reconcile duplicates the create-create race let through.
#
# Callers sleep a jittered interval before their check-then-act, which narrows
# the window when a matrix workflow's legs trip at near-identical times but
# cannot close it: two legs can still both read the list as empty within the
# few seconds the index takes to reflect a fresh create, and each files its
# own. So settle for the index, list every open issue on the label, keep the
# lowest-numbered, and close the rest. Idempotent and convergent — every
# racing leg computes the same keeper, and closing an already-closed
# duplicate is a no-op.
run_issue_create_and_reconcile() {
  local label=$1 title=$2 body_file=$3
  gh issue create --title "$title" --label "$label" -F "$body_file"

  sleep 5
  local open keep
  open=$(gh issue list --label "$label" --state open --json number --jq 'sort_by(.number) | .[].number')
  keep=$(echo "$open" | head -1)
  echo "$open" | tail -n +2 | while read -r dup; do
    [ -z "$dup" ] && continue
    gh issue close "$dup" \
      --comment "Duplicate of #${keep} (concurrent run); consolidating tracking there." \
      2>/dev/null || true
  done
}
