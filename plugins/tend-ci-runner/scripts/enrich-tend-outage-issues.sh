#!/usr/bin/env bash
# Enriches open tend-outage issues with failure details from referenced runs.
#
# The harness action's "Report failure" step records only the workflow run link —
# error annotations and job logs are not reliably available while the job is
# in_progress, so the action can't extract them at the time of failure.
#
# This script runs nightly: for each open tend-outage issue, it finds run IDs
# in the body and comments, fetches failure annotations for each failed job
# in those runs, and posts a single comment per issue with details for every
# newly-enriched run. Already-processed runs are skipped via an
# `<!-- enriched-run:RUN_ID -->` marker — the marker is posted even when no
# annotations were found, so unenrichable runs aren't retried every night.
#
# Per-issue batching: a single nightly invocation may find dozens of unenriched
# runs (e.g. when a failing workflow accumulated rows during the day). Posting
# one comment per run produces a flood — visible spam, plus one sibling
# `issue_comment` event per comment that spins up (and skips) `tend-mention`
# at runner-minute cost. Collapsing into one comment per issue per invocation
# avoids both.

set -euo pipefail

LABEL="tend-outage"
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')

gh issue list --label "$LABEL" --state open --json number --jq '.[].number' \
  | while read -r ISSUE; do
    RAW=$(gh issue view "$ISSUE" --repo "$REPO" --json body,comments)

    REFERENCED=$(echo "$RAW" | jq -r '
      [.body, (.comments[].body)] | .[] | scan("/actions/runs/([0-9]+)")[0]
    ' | sort -u)
    ENRICHED=$(echo "$RAW" | jq -r '
      .comments[].body | scan("<!-- enriched-run:([0-9]+) -->")[0]
    ' | sort -u)

    : > /tmp/enrich-batch.md

    comm -23 <(echo "$REFERENCED") <(echo "$ENRICHED") \
      | while read -r RUN_ID; do
        [ -z "$RUN_ID" ] && continue

        : > /tmp/enrich-errors.md
        # Capture jobs first so a 404 (deleted/expired run) doesn't trip
        # `set -e` via the pipe's exit status.
        JOBS=$(gh api "repos/$REPO/actions/runs/$RUN_ID/jobs" \
          --jq '.jobs[] | select(.conclusion == "failure") | "\(.id)\t\(.name)"' \
          2>/dev/null || true)
        while IFS=$'\t' read -r JOB_ID JOB_NAME; do
          [ -z "$JOB_ID" ] && continue
          MSG=$(gh api "repos/$REPO/check-runs/$JOB_ID/annotations" \
            --jq '[.[] | select(.annotation_level == "failure") | .message
                  | select(test("^Process completed") | not)] | join("\n\n")' \
            2>/dev/null || true)
          [ -n "$MSG" ] && printf '#### %s\n\n```\n%s\n```\n\n' "$JOB_NAME" "$MSG" \
            >> /tmp/enrich-errors.md
        done <<< "$JOBS"

        # A job that fails by a plain non-zero exit — every test suite — writes
        # one annotation, `Process completed with exit code 1.`, which the
        # filter above drops because it names no cause. Its diagnosis is in the
        # log instead, so read the log's tail before giving up. One call per
        # run, not per job, hence outside the loop above.
        if [ ! -s /tmp/enrich-errors.md ]; then
          gh run view "$RUN_ID" --repo "$REPO" --log-failed \
            > /tmp/enrich-log.txt 2>/dev/null || true
          # The last `##[error]` is the anchor: the tool's own output is above
          # it, runner cleanup below.
          END=$(grep -n '##\[error\]' /tmp/enrich-log.txt | tail -1 | cut -d: -f1) || true
          if [ -n "$END" ]; then
            START=$(( END > 30 ? END - 30 : 1 ))
            {
              # A four-backtick fence so a log line that is itself a fence
              # doesn't end the block early.
              printf '#### log tail\n\n````\n'
              # Drop the `job<TAB>step<TAB>` prefix, the byte-order mark the
              # first line of each step carries ahead of its timestamp, the
              # timestamp itself, and the colour escapes — which the log API
              # hands back as the literal characters `^[[42m`, so nothing
              # downstream renders them. Then bound a pathological single line.
              sed -n "${START},${END}p" /tmp/enrich-log.txt \
                | cut -f3- \
                | sed 's/^\xef\xbb\xbf//; s/^[0-9T:.Z-]*Z //; s/\^\[\[[0-9;]*[A-Za-z]//g' \
                | cut -c -500
              printf '````\n\n'
            } >> /tmp/enrich-errors.md
          fi
        fi

        RUN_URL="https://github.com/$REPO/actions/runs/$RUN_ID"
        if [ -s /tmp/enrich-errors.md ]; then
          {
            echo "### [Run $RUN_ID]($RUN_URL)"
            echo
            cat /tmp/enrich-errors.md
            echo "<!-- enriched-run:$RUN_ID -->"
            echo
          } >> /tmp/enrich-batch.md
        else
          {
            echo "### [Run $RUN_ID]($RUN_URL)"
            echo
            echo "No failure details could be extracted."
            echo
            echo "<!-- enriched-run:$RUN_ID -->"
            echo
          } >> /tmp/enrich-batch.md
        fi
      done

    # GitHub rejects a comment body over 65536 characters with a 422. Under
    # `set -e` that rejection takes the whole nightly pass down — and because
    # the `enriched-run` markers land only with the comment, the next night
    # rebuilds the identical oversized batch and fails the same way. Truncate instead: the runs that fall off
    # keep no marker and are enriched by a later, smaller batch.
    if [ -s /tmp/enrich-batch.md ]; then
      if [ "$(wc -c < /tmp/enrich-batch.md)" -gt 60000 ]; then
        # `sed '$d'` drops the line `head -c` cut mid-way, so the body can't end
        # on half a UTF-8 character.
        head -c 60000 /tmp/enrich-batch.md | sed '$d' > /tmp/enrich-post.md
        printf '\n_Truncated; the remaining runs are enriched by a later batch._\n' \
          >> /tmp/enrich-post.md
      else
        cp /tmp/enrich-batch.md /tmp/enrich-post.md
      fi
      gh issue comment "$ISSUE" --repo "$REPO" -F /tmp/enrich-post.md
    fi
  done
