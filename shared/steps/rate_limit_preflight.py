"""Abort the run when the bot is creating issues/PRs faster than it should.

Two limits, shared verbatim by both harness actions: a burst limit (20-minute
window) and a daily spike limit (today's volume against a 6-day baseline).

The spike limit is resumable. When it trips, the run files or reopens a
``tend-rate-limit`` issue naming the run it refused, and a maintainer closing
that issue doubles the ceiling for the rest of the UTC day; each further close
doubles it again. Two things follow from that shape:

- Opening the issue is the notice. A bare ``::error::`` annotation lands on a
  job nobody opens, so before this the repo could stop reviewing for hours
  with no signal but a red check on unrelated PRs.
- Closes by the bot itself do not count, which is what makes this a check
  rather than an instruction the bot could be talked out of. GitHub lets only
  the author or a triage/write collaborator close an issue, and the bot is the
  author — so excluding the bot leaves exactly the maintainers, with no
  allowlist to keep.

A doubling rather than a flat increment because the ceiling it lifts is itself
proportional (``10 + 2 * daily_avg``): a repo filing 15 a day would need a
close every hour to get through a legitimate spike on a flat bump. At the
formula's floor the two coincide — 10 doubles to 20 — so they only diverge
where a flat bump stops working.

The burst limit is deliberately not resumable: ten PRs in twenty minutes is a
loop rather than a busy day, and there is nothing there to wave through.

Also publishes ``login``/``id`` for the bot, read off the token it already
holds, so nothing downstream has to resolve them again from a configured name
a rename would leave stale.

Inputs (env): ``GITHUB_TOKEN`` (the bot's PAT, for ``gh``),
``GITHUB_REPOSITORY``, ``GITHUB_OUTPUT``, plus the run/event vars ``_issue``
reads.
"""

from __future__ import annotations

import random
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any

import _common
import _issue

PAUSE_LABEL = "tend-rate-limit"
PAUSE_TITLE = "Bot rate limit reached"
PAUSE_DESCRIPTION = "Bot paused on its own rate limit; close to approve"
PAUSE_COLOR = "fbca04"

BURST_LIMIT = 10
BURST_WINDOW = timedelta(minutes=20)
BASELINE_DAYS = 6

# The jitter before the check-then-act, in seconds. It narrows the
# create-create race: sibling jobs trip within seconds of each other, and
# without it each files its own issue.
JITTER = 30

PAUSE_BODY = """\
The bot stopped before doing any work: it has filed more issues and PRs today \
than its spike limit allows, which is the check that catches a runaway loop \
between workflows.

**Closing this issue approves the volume and doubles the ceiling for the rest \
of the UTC day.** Each further close doubles it again, so the limit keeps \
working after you have used it. Close it only if the activity below is \
expected — and note the bot cannot approve itself: closes by its own account, \
or by any GitHub App, are not counted.

{row}

The runs listed above were refused and do not retry on their own; re-run them \
with `gh run rerun <id> --failed` once this is closed.
"""

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_DAY = "%Y-%m-%d"


def main() -> int:
    env = _common.require_env(
        "GITHUB_REPOSITORY",
        "GITHUB_OUTPUT",
        "GITHUB_SERVER_URL",
        "GITHUB_RUN_ID",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
    )
    repo = env["GITHUB_REPOSITORY"]

    # Who the bot is comes from the credential, not from configuration: this
    # runs as the bot, so the authenticated user is the bot by definition. A
    # configured name could be misspelled or left stale by a rename, and every
    # way that went wrong failed open — the counts below would match nothing
    # and never trip, and the approval filter would match every close,
    # including the bot's own.
    #
    # The id is what the approval filter compares, because the bot account is
    # an ordinary user account rather than a GitHub App: the type check in
    # `count_approvals` does nothing for it, so identifying it is the whole
    # control.
    try:
        identity = _common.gh_json("api", "user")
        bot, bot_id = identity["login"], int(identity["id"])
    except (*_common.GH_READ_FAILED, KeyError, TypeError, ValueError):
        return _common.fail(
            "Rate limit preflight: could not read the bot's own identity from the "
            "token, so the limit cannot be enforced."
        )

    # Published for the rest of the job, which needs the same two facts.
    _common.set_output("login", bot)
    _common.set_output("id", str(bot_id))

    now = _common.utcnow()
    today = now.strftime(_DAY)
    since = (now - BURST_WINDOW).strftime(_STAMP)

    recent_prs = count_recent_prs(
        _items(f"repos/{repo}/pulls?state=all&sort=created&direction=desc&per_page=30"),
        bot,
        since,
    )
    recent_issues = count_recent_issues(
        _items(
            f"repos/{repo}/issues?creator={bot}&state=all&sort=created"
            "&direction=desc&per_page=30"
        ),
        since,
    )
    today_posts = _total_count(search_path(repo, bot, today))
    past_posts = _total_count(search_path(repo, bot, baseline_range(now)))
    limit = spike_limit(past_posts)

    _common.log(
        "rate-limit",
        f"burst={recent_prs} PRs, {recent_issues} issues (20min); "
        f"today={today_posts} (limit: {limit})",
    )

    abort = False
    if recent_prs > BURST_LIMIT:
        _common.annotate(
            "error",
            f"Rate limit: bot created {recent_prs} PRs in the last 20 minutes "
            f"(limit: {BURST_LIMIT})",
        )
        abort = True
    if recent_issues > BURST_LIMIT:
        _common.annotate(
            "error",
            f"Rate limit: bot created {recent_issues} issues in the last 20 minutes "
            f"(limit: {BURST_LIMIT})",
        )
        abort = True

    # Everything below runs only once the base limit is already exceeded, so
    # the common path costs no extra API calls at all.
    if today_posts > limit:
        # Whether the read succeeded is kept, not just what it returned: a
        # failure and "none open" both come back as no number, and further
        # down that difference decides whether filing is safe. For the
        # approval count the two are equivalent — both leave it at zero, which
        # refuses the run either way.
        lookup_ok = True
        try:
            pause = _issue.canonical(PAUSE_LABEL, "all", PAUSE_TITLE)
        except _common.GH_READ_FAILED:
            pause, lookup_ok = None, False

        approvals = _approvals(repo, pause, bot_id, today) if pause is not None else 0
        ceiling = limit << approvals

        if today_posts <= ceiling:
            _common.log(
                "rate-limit",
                f"{today_posts} today is over the base limit of {limit}, allowed by "
                f"{approvals} approval(s) on #{pause} (ceiling {ceiling})",
            )
        else:
            headline = (
                f"Rate limit: bot created {today_posts} items today, above the "
                f"ceiling of {ceiling} (base limit {limit}, {approvals} approval(s), "
                f"baseline {past_posts} over past {BASELINE_DAYS} days)"
            )
            # Only file when the spike is the whole reason this run is being
            # refused. A burst trip is not resumable, so an issue offering to
            # lift the ceiling would promise a recovery that closing it cannot
            # deliver; the burst annotation above is the honest signal, and no
            # row is owed for a run whose retry would be refused again on the
            # same grounds.
            if abort:
                _common.annotate("error", f"{headline}.")
            else:
                recovery = _file_pause_issue(_issue.row(), pause, lookup_ok)
                _common.annotate("error", f"{headline}. {recovery}")
            abort = True

    if abort:
        return 1
    _common.log("rate-limit", "check passed")
    return 0


def search_path(repo: str, bot: str, created: str) -> str:
    """The ``search/issues`` query counting what the bot filed in *created*.

    ``search/issues`` covers both issues and PRs. The bot's own bookkeeping
    issues (``tend-outage``, ``tend-rate-limit``) are counted like anything
    else it files: a flood of them is itself a plausible runaway — a bot stuck
    in a failure loop files them — so excluding them would blind the metric to
    one of the shapes it exists to catch.
    """
    return f"search/issues?q=author:{bot}+repo:{repo}+created:{created}"


def baseline_range(now: datetime) -> str:
    """The ``created:`` clause covering the six whole days before today."""
    first = (now - timedelta(days=BASELINE_DAYS)).strftime(_DAY)
    last = (now - timedelta(days=1)).strftime(_DAY)
    return f"{first}..{last}"


def spike_limit(past_posts: int) -> int:
    """``10 + 2 * daily_avg``, i.e. ``10 + past_posts / 3`` over six days."""
    return 10 + past_posts // 3


def count_recent_prs(pulls: list[dict[str, Any]], bot: str, since: str) -> int:
    """PRs the bot opened after *since*, an ISO-8601 UTC stamp."""
    return sum(
        1
        for pull in pulls
        if (pull.get("user") or {}).get("login") == bot
        and (pull.get("created_at") or "") > since
    )


def count_recent_issues(issues: list[dict[str, Any]], since: str) -> int:
    """Issues (not PRs) created after *since*; the endpoint filters the author.

    ``/issues`` serves pull requests too, distinguished only by a
    ``pull_request`` member, so dropping those is what keeps a PR from being
    counted against both limits at once.
    """
    return sum(
        1
        for issue in issues
        if issue.get("pull_request") is None and (issue.get("created_at") or "") > since
    )


def count_approvals(
    events: list[dict[str, Any]], *, label: str, bot_id: int, today: str
) -> int:
    """Closes by a person, after the label went on, and today.

    Today, because the ceiling it lifts resets at the UTC rollover along with
    the count itself. After the label, because otherwise moving the label onto
    an issue closed at any earlier point would import that close as an
    approval — and the bot can move labels. On the issue the preflight files
    the label goes on at creation, so this excludes nothing real.

    The two actor exclusions cover different things: the id rules out the bot,
    and ``type != "Bot"`` rules out GitHub Apps — ``github-actions[bot]``
    above all, which a workflow's own ``GITHUB_TOKEN`` acts as, and which is a
    different account that would otherwise read as a person. Excluding the
    class rather than naming apps needs no allowlist.
    """
    since = f"{today}T00:00:00Z"
    for event in events:
        labelled = event.get("event") == "labeled"
        if labelled and (event.get("label") or {}).get("name") == label:
            at = event.get("created_at") or ""
            since = max(since, at)
    return sum(1 for event in events if _is_approval(event, bot_id=bot_id, since=since))


def _is_approval(event: dict[str, Any], *, bot_id: int, since: str) -> bool:
    if event.get("event") != "closed":
        return False
    actor = event.get("actor") or {}
    if actor.get("id") == bot_id or actor.get("type") == "Bot":
        return False
    return (event.get("created_at") or "") > since


def _file_pause_issue(row: str, pause: int | None, lookup_ok: bool) -> str:
    """Reopen and append to the pause issue, or file one; say what to close.

    Returns the recovery sentence the refusal annotation ends with, because
    how far this got and what a maintainer can do about it are the same fact.
    Splitting them apart invites the one wrong answer that matters: saying
    "could not be filed" about an issue that *was* filed sends a maintainer
    away from the one thing that would restart the bot.
    """
    if pause is None:
        # Only look again when there is nothing to append to. The jitter
        # narrows the create-create race, so it buys nothing once the issue is
        # known to exist, and the lookup that found it is still good.
        #
        # A failed re-read leaves the first read's verdict standing rather
        # than clearing it, because the two rule out different things. The
        # first excludes an already-open issue of any age — the duplicate that
        # matters here, since the lookup resolves to the lowest-numbered
        # issue, so a maintainer closing the newer one, the issue this run's
        # annotation names, would approve nothing and never learn it. The
        # re-read only narrows the seconds-wide sibling race, and
        # `_issue.create_and_reconcile`'s downward probe already catches that.
        # So only a run that never managed a good read has to hold off filing;
        # holding off on a failed re-read would pause the bot with no issue at
        # all, which is the outcome opening one exists to avoid.
        time.sleep(random.randrange(JITTER))
        try:
            pause = _issue.canonical(PAUSE_LABEL, "all", PAUSE_TITLE)
            lookup_ok = True
        except _common.GH_READ_FAILED:
            pause = None

    if pause is not None:
        # A no-op when it is already open, which is the usual case for the
        # second and later runs refused in one incident.
        try:
            _common.gh("issue", "reopen", str(pause))
        except subprocess.CalledProcessError:
            pass
        # The row is best-effort, and deliberately so: this is the common path
        # — every refusal after the first in one incident takes it — and it
        # fails under the same secondary rate limit that can refuse a create.
        # The annotation is worth more than the row: the issue already exists,
        # so it can still name what to close.
        try:
            _issue.comment(pause, row)
        except subprocess.CalledProcessError:
            _common.annotate("warning", f"Could not append this run's row to #{pause}.")
        return f"Refused runs are listed in #{pause}; closing it doubles the ceiling."

    if not lookup_ok:
        # Neither read succeeded, so whether an issue is already open is
        # unknown; file nothing. See the re-read above for why a second issue
        # is worse here than none. An issue may well be open and worth closing
        # — this run just never managed to see it — so unlike a failed create,
        # naming the label is all this can offer toward it.
        return (
            "This repo's issues could not be read (see the error above), so none was "
            f"filed; if an open `{PAUSE_LABEL}` issue exists, closing it doubles the "
            "ceiling."
        )

    _issue.ensure_label(PAUSE_LABEL, PAUSE_DESCRIPTION, PAUSE_COLOR)
    # The row twice over: it seeds the body, and it goes to the reconcile as
    # the carry-over argument, so that a leg standing down to a racing sibling
    # moves the row onto the keeper before closing. Skipping it there would
    # strand the refused run's only record in a closed duplicate while the
    # `::error::` names the survivor — and the survivor is the issue whose
    # close lifts the ceiling, so the one artifact a maintainer is asked to
    # act on would be the one missing the evidence.
    try:
        number = _issue.create_and_reconcile(
            PAUSE_LABEL, PAUSE_TITLE, row, PAUSE_BODY.format(row=row)
        )
    except subprocess.CalledProcessError:
        # A failed create is not far-fetched here: this runs when the bot is
        # already at abnormal volume, which is when GitHub is likeliest to
        # answer `issue create` with a secondary rate limit. It must not cost
        # the run its annotation, this run's only trace.
        return (
            "The pause issue could not be filed (see the error above), so there is "
            "nothing to close and the ceiling holds until the UTC rollover."
        )
    if number is None:
        # Filed, but no number came back: `gh issue create` printed something
        # other than an issue URL. The label points at the same issue just as
        # uniquely, where naming `#` would point at nothing.
        return (
            f"Refused runs are listed in the open `{PAUSE_LABEL}` issue; closing it "
            "doubles the ceiling."
        )
    return f"Refused runs are listed in #{number}; closing it doubles the ceiling."


def _total_count(path: str) -> int:
    """``total_count`` from a search, or 0 when the read fails.

    Zero is the safe direction: it can only lower a count, never lift a limit.
    """
    try:
        return int(_common.gh_json("api", path)["total_count"])
    except (*_common.GH_READ_FAILED, KeyError, TypeError, ValueError):
        return 0


def _items(path: str) -> list[dict[str, Any]]:
    """A list endpoint's items, or none when the read fails."""
    try:
        items = _common.gh_json("api", path)
    except _common.GH_READ_FAILED:
        return []
    return items if isinstance(items, list) else []


def _approvals(repo: str, issue: int, bot_id: int, today: str) -> int:
    """Approvals recorded on *issue*, or 0 when the events cannot be read.

    ``/events`` rather than ``/timeline``: it carries every field
    :func:`count_approvals` reads and excludes comments, which matter because
    this issue accumulates one per refused run and is never replaced. Failing
    to read them leaves approvals at zero, which refuses the run — the safe
    direction for a check whose job is to stop things.
    """
    try:
        events = _common.gh_paginated(
            f"repos/{repo}/issues/{issue}/events?per_page=100"
        )
    except _common.GH_READ_FAILED:
        return 0
    return count_approvals(events, label=PAUSE_LABEL, bot_id=bot_id, today=today)


if __name__ == "__main__":
    _common.run(main)
