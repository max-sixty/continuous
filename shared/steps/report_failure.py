"""File or append to a `tend-outage` issue when a run fails.

Shared verbatim by both harness actions; the caller gates it on the job being
red, so a failure anywhere in the action — the rate-limit preflight and
sandbox build ahead of the agent as much as the agent itself — lands in the
tracker. The one exclusion is the security preflight: that failure is a
persistent config refusal rather than an outage, and the issue this files
("temporarily unavailable", closed once resolved) would never resolve.

Just records the run link. Error annotations and logs are not reliably
available while the job is in_progress, so the nightly skill enriches these
issues after the fact, when the run has completed and the APIs return stable
data.

A closed outage issue is left closed and a fresh one filed: closing it means
the `review-runs` drain step read every row and re-ran what needed it, and
reopening would fold the next incident into a stale record. That drain owns
the close — the nightly skill's resolved-issue rule carves this label out,
because nightly's cron precedes review-runs' and "nothing has failed since"
says nothing about the stranded triggers the rows name. Where an adopter
disables `review-runs`, nothing substitutes for it and the close falls to a
maintainer against the same criterion — which is why the issue body states the
criterion rather than naming the sweep as the only route. The rate-limit issue
takes the opposite policy, for reasons in ``_issue.py``.

Repeated appends are bounded per run, not per incident: a matrix workflow runs
this once per leg and every leg shares one ``GITHUB_RUN_ID``, so the tracker
takes at most one row per run. The guard is a check-then-act, so
:func:`duplicate_rows` reconciles the legs that raced it down to that one row.

Inputs (env): ``GITHUB_TOKEN`` (for ``gh``), ``GITHUB_SERVER_URL``,
``GITHUB_REPOSITORY``, ``GITHUB_RUN_ID``, ``GITHUB_EVENT_NAME``,
``GITHUB_EVENT_PATH`` (from Actions).
"""

from __future__ import annotations

import random
import subprocess
import time
from typing import Any

import _common
import _issue

LABEL = "tend-outage"
TITLE = "Bot temporarily unavailable"
LABEL_DESCRIPTION = "Tracks bot outage incidents"
LABEL_COLOR = "d93f0b"

# The jitter before the check-then-act, in seconds, and the settle before the
# reconcile reads back what it and its racing siblings just posted.
JITTER = 30
SETTLE = 5

OUTAGE_BODY = """\
The bot failed to process a request. This issue tracks failures until the \
underlying cause is resolved.

{row}

This issue was created automatically. Close it once every row above is \
drained — each stranded trigger re-run, or confirmed no longer needed. The \
`tend-review-runs` sweep does that where it runs.
"""


def main() -> int:
    env = _common.require_env(
        "GITHUB_REPOSITORY",
        "GITHUB_SERVER_URL",
        "GITHUB_RUN_ID",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
    )
    repo, run_id = env["GITHUB_REPOSITORY"], env["GITHUB_RUN_ID"]
    row = _issue.row()
    anchor = _issue.anchor()

    _issue.ensure_label(LABEL, LABEL_DESCRIPTION, LABEL_COLOR)

    # Jittered backoff before the check-then-act narrows the race window when
    # a matrix workflow's legs fail at near-identical times (e.g. model-API
    # 5xx responses exhausting the retry budget across every leg within a few
    # seconds). Without it every leg reads the list as empty in parallel and
    # each files its own outage issue.
    time.sleep(random.randrange(JITTER))

    # A failed read is not "nothing is open". Filing on it is how a repo ends
    # up with two open trackers, and the reconcile does not clean this one up
    # — it probes the ten numbers below the issue it just filed, and an
    # already-open tracker is normally much older. Two of them scatter later
    # rows across both, so no tracker carries the complete set the drain sweep
    # needs. Skipping costs this one row on a transient failure, and the next
    # failure records normally.
    try:
        existing = _issue.canonical(LABEL, "open", TITLE)
    except _common.GH_READ_FAILED:
        _common.annotate(
            "warning",
            f"Could not read this repo's {LABEL} issues, so this run was not "
            "recorded on the outage tracker.",
        )
        return 0

    if existing is None:
        # With no tracker open there is no other record of the outage, so a
        # failed create has to redden the step. The rate-limit caller guards
        # both of its writes instead: a failed create there must still reach
        # the annotation that names what to close, where here the create is
        # the last statement and the red step is all that is left.
        try:
            _issue.create_and_reconcile(LABEL, TITLE, row, OUTAGE_BODY.format(row=row))
        except subprocess.CalledProcessError as err:
            return _common.fail(
                f"Could not file the {LABEL} tracker: "
                f"{(err.stderr or '').strip() or err}"
            )
        return 0

    # Per-run comment dedup. Skip if this run is already recorded, whether in
    # the issue body (a leg of this same run seeded the issue) or in an
    # existing comment. A read that outright fails falls through to posting,
    # which risks a duplicate row rather than losing one.
    if anchor in _issue.recorded_text(existing):
        _common.log(
            "report-failure",
            f"run {run_id} already recorded on #{existing} — skipping duplicate "
            "comment",
        )
        return 0

    # The common path once a tracker is open — every failure after the first
    # in one incident appends through here — and it can 5xx like any other
    # write. Aborting would drop the row without saying so: the tracker then
    # under-reports the outage, and a run stranded by it reads as one that
    # never happened. A leg that failed to post has nothing of its own to
    # reconcile, so it stops here; any racing leg that did post reconciles on
    # its own pass.
    try:
        _issue.comment(existing, row)
    except subprocess.CalledProcessError:
        _common.annotate("warning", f"Could not append this run's row to #{existing}.")
        return 0

    _reconcile(repo, existing, anchor)
    return 0


def duplicate_rows(comments: list[dict[str, Any]], anchor: str) -> list[int]:
    """Every generated row for this run but the earliest — the ones to delete.

    The check-then-act above races across concurrently-jittered legs: two can
    both read no matching row before either posts. Convergent, like the create
    reconcile in ``_issue.create_and_reconcile``: every racing leg sorts the
    same way and computes the same keeper, so deleting an already-deleted
    comment is a harmless 404.

    Selecting on the anchor rather than the bare run URL keeps only the bot's
    own generated rows eligible for deletion — a person linking the run in
    discussion, which is the normal way an outage gets diagnosed, is not a
    duplicate to be removed.
    """
    rows = [c for c in comments if anchor in (c.get("body") or "")]
    rows.sort(key=lambda c: c.get("created_at") or "")
    return [c["id"] for c in rows[1:]]


def _reconcile(repo: str, issue: int, anchor: str) -> None:
    """Collapse this run's rows on *issue* down to the earliest one.

    Paginated, because this endpoint returns comments oldest-first and the
    issues that need reconciling are exactly the flooded ones: past 100
    comments the rows just posted fall off the first page, and an unpaginated
    read would find nothing to reconcile on the only issues where it matters.

    Best-effort throughout: this runs *after* the row landed, so a 5xx here
    would otherwise redden the step with no annotation saying why, on
    precisely the job someone is about to diagnose. Duplicate rows on the
    tracker are the cheaper loss.

    "Throughout" is the envelope's scope rather than a claim to check by
    reading: it wraps the read, the selection and the deletes together, so a
    later call added anywhere in here is covered by construction. The deletes
    keep their own guard inside it, because a duplicate a racing leg already
    removed answers 404 and must not stop the loop.
    """
    time.sleep(SETTLE)
    try:
        comments = _common.gh_paginated(
            f"repos/{repo}/issues/{issue}/comments?per_page=100"
        )
        for comment_id in duplicate_rows(comments, anchor):
            try:
                _common.gh(
                    "api", "-X", "DELETE", f"repos/{repo}/issues/comments/{comment_id}"
                )
            except subprocess.CalledProcessError:
                pass
    except _common.GH_READ_FAILED:
        _common.annotate(
            "warning",
            f"Could not reconcile duplicate rows on #{issue}; this run's row is "
            "recorded.",
        )


if __name__ == "__main__":
    _common.run(main)
