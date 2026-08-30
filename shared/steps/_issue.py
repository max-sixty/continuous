"""Shared machinery for the issues the actions file about their own runs.

``tend-outage`` (``report_failure.py``) and ``tend-rate-limit``
(``rate_limit_preflight.py``) both name the run and the trigger it stranded in
one table format, and both race with sibling jobs failing or tripping at the
same instant.

Callers keep their own policy about a closed issue, because the two differ and
the difference is deliberate: an outage issue closed as resolved must not
swallow the next incident, so ``report_failure`` files a fresh one; the
rate-limit issue is a single long-lived record whose closes *are* the
approvals, so the preflight reopens it.

Inputs (env, from Actions): ``GITHUB_SERVER_URL``, ``GITHUB_REPOSITORY``,
``GITHUB_RUN_ID``, ``GITHUB_EVENT_NAME``, ``GITHUB_EVENT_PATH``.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

import _common

# How far below its own number a leg probes for a racing sibling. Issue numbers
# are monotonic, so any sibling sits just below ours.
PROBE_WINDOW = 10

_ROW_HEADER = "| When | Run | Trigger |\n|------|-----|---------|"


def ref() -> str:
    """A one-line reference to the triggering context, for the Trigger column.

    This is the only pointer back to the work a refused or failed run
    stranded, so every trigger that carries one names it. Empty for events
    with no thread of their own (``schedule``, ``workflow_dispatch``); the
    caller renders that as ``N/A``.
    """
    if os.environ["GITHUB_EVENT_NAME"] == "workflow_run":
        # Link the run being fixed — without its id there is no way back to
        # the failure the ci-fix job was dispatched to handle.
        run_id = _common.dig(_common.event_payload(), "workflow_run", "id")
        if not run_id:
            return "CI fix for workflow run"
        return f"CI fix for [run {run_id}]({_run_url(run_id)})"
    number = _common.subject_number()
    return f"#{number}" if number else ""


def _run_url(run_id: Any) -> str:
    env = os.environ
    return (
        f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{run_id}"
    )


def anchor() -> str:
    """The Run cell's link to this run, as it appears in a row.

    One definition, because two things have to agree on it exactly: the row
    :func:`row` writes, and the dedup that recognises a row already recorded
    for this run. A whole anchor rather than the bare URL, so a human comment
    merely mentioning the run can't be mistaken for a generated row, and a
    longer run id carrying this one as a prefix can't match it.
    """
    return f"[workflow run]({_run_url(os.environ['GITHUB_RUN_ID'])})"


def row() -> str:
    """One row per incident, in the same table format wherever it lands.

    Whether it seeds an issue body (the first one) or is appended as a comment
    (every later one), both render identically. Stamps the time when called —
    capture it once per run.
    """
    when = _common.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{_ROW_HEADER}\n| {when} | {anchor()} | {ref() or 'N/A'} |"


def matching(label: str, state: str, title: str) -> list[int]:
    """Every issue this bot filed under *title* carrying *label*, lowest first.

    A label no repo has yet returns an empty list rather than an error.
    """
    issues = _common.gh_json(
        "issue",
        "list",
        "--label",
        label,
        "--state",
        state,
        "--author",
        "@me",
        "--limit",
        "100",
        "--json",
        "number,title",
    )
    return sorted(issue["number"] for issue in issues if issue.get("title") == title)


def canonical(label: str, state: str, title: str) -> int | None:
    """The one that counts: lowest-numbered, ``None`` when there is none.

    Lowest rather than newest, because a race can leave a duplicate the
    reconcile then closes and the lowest is the one every leg computes alike.

    Raises ``CalledProcessError`` when the list read itself fails. "The read
    failed" and "there is none" are different facts, and a caller that
    conflates them files a fresh record while one is already open.
    """
    numbers = matching(label, state, title)
    return numbers[0] if numbers else None


def ensure_label(label: str, description: str, color: str) -> None:
    """Best-effort: the label exists on every repo after the first incident,
    and ``gh label create`` has no idempotent form."""
    try:
        _common.gh(
            "label", "create", label, "--description", description, "--color", color
        )
    except subprocess.CalledProcessError:
        pass


def comment(number: int, body: str) -> None:
    """Post *body* on issue *number*; raises when the post fails."""
    _common.gh(
        "issue",
        "comment",
        str(number),
        "-F",
        "-",
        input=body if body.endswith("\n") else body + "\n",
    )


def recorded_text(number: int) -> str:
    """The issue's body and every comment body, joined — what a dedup reads.

    A failed read returns the empty string, which reads as "not recorded" and
    falls through to posting: a duplicate row is the cheaper loss.
    """
    try:
        issue = _common.gh_json("issue", "view", str(number), "--json", "body,comments")
    except _common.GH_READ_FAILED:
        return ""
    bodies = [issue.get("body") or ""]
    bodies += [c.get("body") or "" for c in issue.get("comments") or []]
    return "\n".join(bodies)


def is_ours(issue: dict[str, Any], *, title: str, label: str, login: str) -> bool:
    """Whether *issue* is an open record this bot filed under *title*/*label*.

    The same three constraints :func:`matching` applies — author, title, label
    — checked one issue at a time. Two predicates rather than one because the
    call shapes differ: ``matching`` scopes a listing server-side, this reads a
    single issue object. Nothing holds them in step, so a change to either
    belongs in both. Author above all: the bot holds ``issues: write``, so
    without it a label put on somebody else's issue could be adopted as the
    keeper, and on the rate-limit record a close is read as an approval.
    """
    return (
        issue.get("state") == "open"
        and issue.get("title") == title
        and (issue.get("user") or {}).get("login") == login
        and any(entry.get("name") == label for entry in issue.get("labels") or [])
    )


def create_and_reconcile(label: str, title: str, row: str, body: str) -> int | None:
    """Create the issue, stand down to a racing sibling, return the keeper.

    Raises ``CalledProcessError`` when the create itself fails; returns
    ``None`` in the one case where the issue was filed but its number could
    not be read off the URL, so the caller can say "the open ``<label>``
    issue" rather than name a number that does not exist.

    *row* is this run's row, and it is required rather than optional: when
    this leg stands down it carries that row onto the keeper first, so an
    incident it recorded is not stranded in the body of a closed duplicate. A
    caller that omitted it would strand the very record this exists to
    preserve.

    Callers sleep a jittered interval before their check-then-act, which
    narrows the window when sibling jobs trip at near-identical times but
    cannot close it: two legs can still both read the list as empty within the
    few seconds the index takes to reflect a fresh create, and each files its
    own.

    The probe goes downwards rather than re-listing, because a settle-then-list
    reconcile reads the same lagging index that lost the race in the first
    place: observed in practice, a sibling created 3 s earlier was still absent
    from the list while one created 6 s earlier was present, so two legs whose
    creates landed in the same second each read back only their own issue and
    closed nothing. ``GET /issues/{n}`` is a primary-key read and returns a
    sibling the instant it exists. Deferring to the *lowest* match rather than
    the nearest is what makes it converge: every leg computes the same keeper
    from its own vantage point, and only higher-numbered legs stand down.

    Self-close-only is a deliberate narrowing: closing every higher duplicate
    a leg could see only ever worked when the lagging list happened to be
    current, which is the very thing that failed in production. This fails
    toward an extra open record rather than scattered rows.
    """
    url = _common.gh(
        "issue", "create", "--title", title, "--label", label, "-F", "-", input=body
    ).strip()
    _common.log(label, f"filed {url}")

    tail = url.rsplit("/", 1)[-1]
    if not tail.isdigit():
        return None
    mine = int(tail)

    # Failing to read our own identity leaves the pair open rather than
    # deferring to an issue we cannot vouch for.
    try:
        me = _common.gh_json("api", "user")["login"]
    except (*_common.GH_READ_FAILED, KeyError):
        return mine

    repo = os.environ["GITHUB_REPOSITORY"]
    keep: int | None = None
    for number in range(mine - 1, max(mine - PROBE_WINDOW - 1, 0), -1):
        try:
            candidate = _common.gh_json("api", f"repos/{repo}/issues/{number}")
        except _common.GH_READ_FAILED:
            continue
        if is_ours(candidate, title=title, label=label, login=me):
            keep = number
    if keep is None:
        return mine

    # Carry the row over only when the keeper does not already cite this run.
    # Matrix legs share one GITHUB_RUN_ID, so when the racing legs belong to
    # the same matrix the keeper's seed row already points at this run and a
    # carried row would just duplicate it, differing only in its timestamp.
    # The cross-workflow race has distinct run ids, so it still carries over.
    if anchor() not in recorded_text(keep):
        try:
            comment(keep, row)
        except subprocess.CalledProcessError:
            pass
    try:
        _common.gh(
            "issue",
            "close",
            str(mine),
            "--comment",
            f"Duplicate of #{keep} (concurrent run); consolidating tracking there.",
        )
    except subprocess.CalledProcessError:
        pass
    return keep
