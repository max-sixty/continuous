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

        # GitHub rejects a comment body over 65536 characters with a 422, and
        # under `set -e` that rejection takes the whole nightly pass down —
        # then, because the `enriched-run` markers land only with the comment,
        # the next night rebuilds the identical batch and fails identically.
        # Stop between runs instead, so every posted section keeps its fences
        # and its marker; the runs that fall off here carry no marker and are
        # enriched by a later, smaller batch. A run's own section is bounded
        # above (30 lines x 500 characters per job, at most ~15 KB of jobs),
        # so the finished body cannot exceed roughly 60 KB.
        if [ "$(wc -c < /tmp/enrich-batch.md)" -gt 30000 ]; then
          printf '_Truncated; the remaining runs are enriched by a later batch._\n' \
            >> /tmp/enrich-batch.md
          break
        fi

        : > /tmp/enrich-errors.md
        # Capture jobs first so a 404 (deleted/expired run) doesn't trip
        # `set -e` via the pipe's exit status.
        JOBS=$(gh api "repos/$REPO/actions/runs/$RUN_ID/jobs" \
          --jq '.jobs[] | select(.conclusion == "failure") | "\(.id)\t\(.name)"' \
          2>/dev/null || true)
        while IFS=$'\t' read -r JOB_ID JOB_NAME; do
          [ -z "$JOB_ID" ] && continue
          # One run's own section has to fit the headroom the batch cap below
          # leaves, and a matrix run's failed jobs are otherwise unbounded.
          if [ "$(wc -c < /tmp/enrich-errors.md)" -gt 15000 ]; then
            printf '_Remaining failed jobs omitted._\n\n' >> /tmp/enrich-errors.md
            break
          fi
          # A linter annotates per finding, so bound the message the same way
          # the log tail below is bounded: 30 lines of at most 500 characters.
          MSG=$(gh api "repos/$REPO/check-runs/$JOB_ID/annotations" \
            --jq '[.[] | select(.annotation_level == "failure") | .message
                  | select(test("^Process completed") | not)] | join("\n\n")' \
            2>/dev/null | head -n 30 | cut -c -500 || true)
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

    # An `if` rather than `[ -s ... ] && gh ...`: a false test there is the last
    # command of the loop body, so an issue whose runs are all already enriched
    # would end the pipeline non-zero and take the pass down under `set -e`.
    if [ -s /tmp/enrich-batch.md ]; then
      gh issue comment "$ISSUE" --repo "$REPO" -F /tmp/enrich-batch.md
    fi
  done
