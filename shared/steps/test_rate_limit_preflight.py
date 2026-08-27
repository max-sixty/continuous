from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import _common
import rate_limit_preflight as preflight
from _fakes import FakeGh, GithubFiles

BOT = "tend-agent"
BOT_ID = 4242
NEW_ISSUE = 42
RUN_LINK = "[workflow run](https://github.com/owner/repo/actions/runs/12345)"

# Fixed so the day-scoping assertions are deterministic: "today" is 2026-01-02,
# which puts the burst window's floor at 11:40 and the baseline at the six
# whole days before.
NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
TODAY = "2026-01-02"
LABELLED_AT = f"{TODAY}T08:00:00Z"

PULLS = "repos/owner/repo/pulls?state=all&sort=created&direction=desc&per_page=30"
ISSUES = (
    f"repos/owner/repo/issues?creator={BOT}&state=all&sort=created"
    "&direction=desc&per_page=30"
)
SEARCH_TODAY = f"search/issues?q=author:{BOT}+repo:owner/repo+created:{TODAY}"
SEARCH_BASELINE = (
    f"search/issues?q=author:{BOT}+repo:owner/repo+created:2025-12-27..2026-01-01"
)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preflight jitters before its check-then-act; real sleeps add up."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@pytest.fixture
def gh(
    fake_gh: FakeGh,
    github_files: GithubFiles,
    actions_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeGh:
    """A `gh` wired for the happy path: identity readable, every count under.

    `past=15` puts the base limit at 10 + 15/3 = 15, so a test trips the spike
    by setting today's count above it.
    """
    monkeypatch.setattr(_common, "utcnow", lambda: NOW)
    fake_gh.respond("api", "user", with_={"login": BOT, "id": BOT_ID})
    fake_gh.respond("api", PULLS, with_=[])
    fake_gh.respond("api", ISSUES, with_=[])
    _posts(fake_gh, today=10, past=15)
    fake_gh.respond("issue", "list", with_=[])
    fake_gh.respond(
        "issue", "create", with_=f"https://github.com/owner/repo/issues/{NEW_ISSUE}\n"
    )
    fake_gh.respond("issue", "view", with_={"body": "", "comments": []})
    for verb in ("comment", "reopen", "close"):
        fake_gh.respond("issue", verb, with_="")
    fake_gh.respond("label", "create", with_="")
    # The reconciler probes the ten numbers below the one it just filed; each
    # is a primary-key read that 404s unless a test seeds a sibling there. Ten
    # as a literal rather than off `_issue.PROBE_WINDOW`: a widened window then
    # probes a number nothing seeded, which `FakeGh` refuses.
    for number in range(NEW_ISSUE - 1, NEW_ISSUE - 11, -1):
        fake_gh.respond("api", f"repos/owner/repo/issues/{number}", with_=404)
    return fake_gh


def _posts(gh: FakeGh, *, today: int, past: int) -> None:
    gh.respond("api", SEARCH_TODAY, with_={"total_count": today})
    gh.respond("api", SEARCH_BASELINE, with_={"total_count": past})


def _pause_issue(
    gh: FakeGh,
    *events: dict[str, Any],
    number: int = NEW_ISSUE,
    labelled_at: str = LABELLED_AT,
) -> None:
    """An open pause issue, labelled at *labelled_at*, carrying *events*."""
    gh.respond(
        "issue", "list", with_=[{"number": number, "title": preflight.PAUSE_TITLE}]
    )
    labelled = {
        "event": "labeled",
        "label": {"name": preflight.PAUSE_LABEL},
        "created_at": labelled_at,
    }
    gh.respond(
        "api",
        "--paginate",
        "--slurp",
        f"repos/owner/repo/issues/{number}/events?per_page=100",
        with_=[[labelled, *events]],
    )


def _closed(
    login: str, actor_type: str = "User", day: str = TODAY, actor_id: int = 99
) -> dict[str, Any]:
    return {
        "event": "closed",
        "actor": {"login": login, "id": actor_id, "type": actor_type},
        "created_at": f"{day}T09:00:00Z",
    }


def _probe(gh: FakeGh, number: int, **overrides: Any) -> None:
    """Seed `GET /issues/{number}` with an issue the preflight could have filed."""
    gh.respond(
        "api",
        f"repos/owner/repo/issues/{number}",
        with_={
            "number": number,
            "state": "open",
            "title": preflight.PAUSE_TITLE,
            "user": {"login": BOT},
            "labels": [{"name": preflight.PAUSE_LABEL}],
            **overrides,
        },
    )


def _stdin_for(gh: FakeGh, *prefix: str) -> str:
    """What the first call matching *prefix* was handed on stdin."""
    for call, stdin in zip(gh.calls, gh.stdins, strict=True):
        if call[: len(prefix)] == prefix:
            return stdin or ""
    raise AssertionError(f"no call matching {prefix}: {gh.calls}")


def _serve(response: Any) -> str:
    """One canned `issue list` response, failing the way `FakeGh` would."""
    if isinstance(response, int):
        raise subprocess.CalledProcessError(response, ["gh"], "", "fake gh failure")
    return json.dumps(response)


def test_passes_under_the_limit_and_publishes_the_bots_identity(
    gh: FakeGh, github_files: GithubFiles, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under the base limit nothing is looked up and nothing is filed.

    The two outputs are published either way: the rest of the job reads the
    bot's login and id off this step rather than resolving them again from a
    configured name a rename would leave stale.
    """
    assert preflight.main() == 0
    assert "check passed" in capsys.readouterr().out
    assert not gh.called("issue"), f"touched an issue under the limit: {gh.calls}"
    assert github_files.outputs() == {"login": BOT, "id": str(BOT_ID)}


def test_files_an_issue_when_unapproved(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """Over the limit with no approval: refuse, file the issue that says so,
    and name its number — the annotation is what sends a maintainer to it."""
    _posts(gh, today=16, past=15)

    assert preflight.main() == 1
    out = capsys.readouterr().out
    assert gh.called("issue", "create")
    assert f"#{NEW_ISSUE}" in out
    assert "could not be filed" not in out
    assert "#?" not in out


def test_says_so_when_the_issue_cannot_be_filed(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed create must not be reported as a filed issue.

    The run is refused either way; what is lost is the notice — and an
    annotation naming an issue that does not exist sends a maintainer after
    the one thing that would restart the bot, while it stays halted for the
    UTC day.
    """
    _posts(gh, today=16, past=15)
    gh.respond("issue", "create", with_=1)

    assert preflight.main() == 1
    out = capsys.readouterr().out
    assert "could not be filed" in out
    assert "#?" not in out


def test_keeps_its_annotation_when_the_row_cannot_be_appended(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed comment must not cost the run its annotation.

    The append path is the common one — every refusal after the first in an
    incident takes it. The row is the lesser loss: the issue exists, so the
    annotation can still say what to close.
    """
    _posts(gh, today=16, past=15)
    _pause_issue(gh)
    gh.respond("issue", "comment", with_=1)

    assert preflight.main() == 1
    assert f"Refused runs are listed in #{NEW_ISSUE}" in capsys.readouterr().out


def test_files_nothing_when_the_issue_list_cannot_be_read(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed list read must not be taken for "no issue exists".

    Acting on the wrong reading files a second pause issue, which the
    reconcile cannot merge away: it probes the ten numbers under the issue it
    just filed, and an already-open pause issue is normally far older. The
    duplicate then costs an approval outright — the lookup resolves to the
    lowest-numbered issue, so a maintainer closing the newer one, which is the
    issue this run's annotation names, approves nothing.
    """
    _posts(gh, today=16, past=15)
    gh.respond("issue", "list", with_=1)

    assert preflight.main() == 1
    out = capsys.readouterr().out
    assert not gh.called("issue", "create"), gh.calls
    assert "could not be read" in out
    assert "could not be filed" not in out


def test_still_files_when_only_the_re_read_fails(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed re-read must not suppress the file the first read cleared.

    The two reads rule out different things. The first excludes an
    already-open issue of any age, which is the duplicate worth avoiding; the
    re-read after the jitter only narrows the seconds-wide sibling race, and
    the reconcile's downward probe catches that anyway. Holding off here would
    pause the bot with no issue at all — the outcome opening one exists to
    avoid — and point the maintainer at an issue this run's own first read
    established isn't there.
    """
    _posts(gh, today=16, past=15)
    reads = iter([[], 1])
    gh.respond("issue", "list", with_=lambda argv, stdin: _serve(next(reads)))

    assert preflight.main() == 1
    assert gh.called("issue", "create")
    assert "could not be read" not in capsys.readouterr().out


def test_files_when_only_the_first_read_fails(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """The re-read's verdict counts when the first read never landed.

    The mirror of the case above, and the reason the re-read raises the flag
    rather than merely leaving it alone. Without that raise the run refuses,
    files nothing, and points the maintainer at an open issue the re-read had
    just established isn't there — the same dead end from the other side.
    """
    _posts(gh, today=16, past=15)
    reads = iter([1, []])
    gh.respond("issue", "list", with_=lambda argv, stdin: _serve(next(reads)))

    assert preflight.main() == 1
    assert gh.called("issue", "create")
    assert "could not be read" not in capsys.readouterr().out


def test_human_close_doubles_the_ceiling(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """One close by a person takes the ceiling from 15 to 30."""
    _posts(gh, today=16, past=15)
    _pause_issue(gh, _closed("maintainer"))

    assert preflight.main() == 0
    assert "ceiling 30" in capsys.readouterr().out


def test_bot_cannot_approve_itself(gh: FakeGh) -> None:
    """The security property, end to end: the bot closing its own issue is not
    an approval.

    The bot has `issues: write` and authors this issue, so it *can* close it.
    What stops that being self-approval is `count_approvals`, not an
    instruction in a prompt.
    """
    _posts(gh, today=16, past=15)
    _pause_issue(gh, _closed(BOT, actor_id=BOT_ID))

    assert preflight.main() == 1, (
        "the bot approved itself by closing its own pause issue"
    )


@pytest.mark.parametrize(
    ("events", "expected", "why"),
    [
        ([_closed("maintainer")], 1, "a person's close today is an approval"),
        ([_closed("maintainer"), _closed("other")], 2, "each close doubles again"),
        # A renamed account is still the bot: the account is an ordinary user
        # account, so the type check does nothing for it and the id is the
        # whole control. Matching on a name would fail open the moment the
        # account were renamed — an actor matching nothing reads as a person.
        ([_closed("tend-agent-renamed", actor_id=BOT_ID)], 0, "the bot, renamed"),
        # `github-actions[bot]`, which a workflow's own GITHUB_TOKEN acts as,
        # is a different account that the id check alone would let through.
        ([_closed("github-actions[bot]", actor_type="Bot")], 0, "a GitHub App"),
        # Approvals are scoped to today, since the count they lift resets at
        # the UTC rollover.
        ([_closed("maintainer", day="2026-01-01")], 0, "yesterday's close"),
    ],
)
def test_count_approvals_takes_only_a_persons_close_today(
    events: list[dict[str, Any]], expected: int, why: str
) -> None:
    labelled = {
        "event": "labeled",
        "label": {"name": preflight.PAUSE_LABEL},
        "created_at": "2026-01-01T08:00:00Z",
    }
    counted = preflight.count_approvals(
        [labelled, *events], label=preflight.PAUSE_LABEL, bot_id=BOT_ID, today=TODAY
    )
    assert counted == expected, why


def test_count_approvals_ignores_a_foreign_labels_event() -> None:
    """Only this record's label moves the floor.

    Any label can go on this issue, so a `labeled` event for an unrelated one
    later today would otherwise raise the floor past a genuine approval and
    silently drop it.
    """
    foreign = {
        "event": "labeled",
        "label": {"name": "needs-triage"},
        "created_at": f"{TODAY}T23:00:00Z",
    }
    events = [foreign, _closed("maintainer")]
    assert (
        preflight.count_approvals(
            events, label=preflight.PAUSE_LABEL, bot_id=BOT_ID, today=TODAY
        )
        == 1
    )


def test_count_approvals_ignores_closes_that_predate_the_label() -> None:
    """Moving the label onto an already-closed issue grants nothing.

    The bot holds `issues: write`, so it can label any issue. Were approvals
    counted from the whole history, labelling one a maintainer had closed
    earlier today would import that close as an approval nobody gave. On a
    real pause issue the label goes on at creation, so nothing genuine is
    excluded.
    """
    labelled = {
        "event": "labeled",
        "label": {"name": preflight.PAUSE_LABEL},
        "created_at": f"{TODAY}T10:00:00Z",
    }
    events = [labelled, _closed("maintainer")]  # closed at 09:00, an hour earlier
    assert (
        preflight.count_approvals(
            events, label=preflight.PAUSE_LABEL, bot_id=BOT_ID, today=TODAY
        )
        == 0
    )


def test_reconciler_keeps_only_what_the_preflight_filed(gh: FakeGh) -> None:
    """The reconciler nominates its keeper on the whole predicate.

    On the label alone, any lower-numbered issue carrying it would outrank the
    record just filed, which is then closed as that issue's duplicate — the
    refused-run rows and the `::error::` end up pointing at different issues.
    Each of these sits inside the probe window failing exactly one of author,
    title, label, still-open.
    """
    _posts(gh, today=16, past=15)
    _probe(gh, 41, title="Something a maintainer labelled")
    _probe(gh, 40, labels=[{"name": "unrelated-label"}])
    _probe(gh, 39, user={"login": "someone"})
    _probe(gh, 38, state="closed")

    assert preflight.main() == 1
    assert not gh.called("issue", "close"), (
        f"reconciled against issues the preflight never filed: {gh.calls}"
    )
    assert gh.called("api", "repos/owner/repo/issues/41"), "the reconciler never probed"


def test_reconciler_stands_down_to_a_racing_sibling(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sibling that filed first keeps the record; this leg closes its own.

    The pair only exists because both legs read the list as empty inside the
    window it takes to reflect a fresh create, so the reconcile cannot re-read
    that list — it probes the numbers below its own, which are primary-key
    reads and return the sibling the instant it exists.
    """
    _posts(gh, today=16, past=15)
    _probe(gh, 41)

    assert preflight.main() == 1
    assert gh.called("issue", "close", str(NEW_ISSUE)), (
        f"both legs kept their own record: {gh.calls}"
    )
    # The `::error::` has to name the survivor, not the issue just closed.
    assert "#41" in capsys.readouterr().out


def test_carries_its_row_onto_the_racing_sibling(gh: FakeGh) -> None:
    """Standing down must not strand the refused run's row.

    Here the row *is* the notice: the `::error::` sends the maintainer to the
    survivor, and closing that issue is what lifts the ceiling. So the leg
    that stands down has to move its row across first — otherwise the one
    artifact a person is asked to act on is the one missing the run it
    refused.
    """
    _posts(gh, today=16, past=15)
    _probe(gh, 41)
    # A sibling from another workflow: its seed row cites a different run.
    gh.respond("issue", "view", with_={"body": "run 999 row", "comments": []})

    assert preflight.main() == 1
    assert gh.called("issue", "comment", "41"), (
        f"closed its own record without carrying the row over: {gh.calls}"
    )
    assert gh.called("issue", "close", str(NEW_ISSUE)), gh.calls
    assert RUN_LINK in _stdin_for(gh, "issue", "comment", "41")


def test_skips_the_issue_when_the_burst_limit_refused(gh: FakeGh) -> None:
    """A burst trip files nothing: closing the issue could not lift it.

    The burst limit is deliberately not resumable, so an issue offering to
    double the ceiling would promise a recovery it cannot deliver.
    """
    _posts(gh, today=16, past=15)
    gh.respond(
        "api",
        PULLS,
        with_=[
            {"user": {"login": BOT}, "created_at": "2099-01-01T00:00:00Z"}
            for _ in range(11)
        ],
    )

    assert preflight.main() == 1
    assert not gh.called("issue", "create"), (
        f"filed a rate-limit issue for a burst trip it cannot lift: {gh.calls}"
    )


def test_refuses_to_run_without_an_identity(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unable to read its own identity, the preflight stops rather than guesses.

    Every count and the approval filter are keyed on who the bot is. Carrying
    on without that would leave the counts matching nothing and the filter
    matching every close — a check that has silently reversed rather than
    failed.
    """
    gh.respond("api", "user", with_=1)

    assert preflight.main() == 1
    assert "could not read the bot's own identity" in capsys.readouterr().out


def test_foreign_issue_is_not_the_anchor(gh: FakeGh) -> None:
    """Only an issue the preflight filed anchors the approval.

    The bot holds `issues: write`, so it can label anything. Were the label the
    whole predicate, the lowest-numbered issue carrying it would be nominated
    and a close on it read as an approval nobody gave. The title half runs
    through `_issue.matching`; the author half is a server-side flag, so it is
    asserted on the call the step made.
    """
    _posts(gh, today=16, past=15)
    _pause_issue(gh, _closed("maintainer"))
    gh.respond(
        "issue",
        "list",
        with_=[{"number": 7, "title": "Something a maintainer labelled"}],
    )

    assert preflight.main() == 1, "a foreign issue was taken as the anchor"
    lookups = gh.called("issue", "list")
    assert lookups, "the anchor lookup never ran"
    for call in lookups:
        assert call[call.index("--author") + 1] == "@me", (
            f"the anchor lookup is not scoped to issues the bot authored: {call}"
        )
        assert call[call.index("--state") + 1] == "all", (
            f"the anchor lookup must see a closed issue too: {call}"
        )


def test_reopens_rather_than_refiling(gh: FakeGh) -> None:
    """Past the doubled ceiling the existing issue is reopened, not duplicated."""
    _posts(gh, today=40, past=15)
    _pause_issue(gh, _closed("maintainer"))

    assert preflight.main() == 1
    assert gh.called("issue", "reopen", str(NEW_ISSUE)), gh.calls
    assert not gh.called("issue", "create"), (
        f"filed a second pause issue instead of reopening #{NEW_ISSUE}: {gh.calls}"
    )


def test_the_limits_and_the_queries_they_are_measured_over() -> None:
    """The numbers and windows this check is: change one and a repo's ceiling
    moves, so they are pinned rather than derived in the assertion."""
    assert preflight.spike_limit(0) == 10, "the floor, where a doubling is a +10"
    assert preflight.spike_limit(15) == 15, "10 + 2 * (15 / 6)"
    assert preflight.baseline_range(NOW) == "2025-12-27..2026-01-01"


def test_issue_counts_exclude_pull_requests() -> None:
    """`/issues` serves pull requests too, told apart only by a `pull_request`
    member — counting one here would charge it against both limits at once."""
    since = "2026-01-02T11:40:00Z"
    issues = [
        {"created_at": "2026-01-02T11:50:00Z"},
        {"created_at": "2026-01-02T11:30:00Z"},
        {"created_at": "2026-01-02T11:50:00Z", "pull_request": {"url": "…"}},
    ]
    assert preflight.count_recent_issues(issues, since) == 1


def test_burst_window_is_the_last_twenty_minutes(gh: FakeGh) -> None:
    """Eleven PRs trip the burst limit only if they are inside the window.

    The window is what makes this a burst rather than a busy day, so it is
    measured against the frozen clock rather than asserted on a literal the
    step also computes.
    """
    inside = [
        {"user": {"login": BOT}, "created_at": f"{TODAY}T11:41:00Z"} for _ in range(11)
    ]
    gh.respond("api", PULLS, with_=inside)
    assert preflight.main() == 1

    outside = [
        {"user": {"login": BOT}, "created_at": f"{TODAY}T11:39:00Z"} for _ in range(11)
    ]
    gh.respond("api", PULLS, with_=outside)
    assert preflight.main() == 0


def test_a_blip_that_answers_with_html_leaves_the_counts_at_zero(
    gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """A GitHub blip can answer a request with an HTML error page under a 200.

    `gh` then exits zero and the parse is what fails. The shell body swallowed
    that with the same `|| echo 0` that caught a non-zero exit, and so must
    this: a blip must degrade the counts, not crash the gate on every run and
    file an outage row for each.
    """
    for path in (PULLS, ISSUES, SEARCH_TODAY, SEARCH_BASELINE):
        gh.respond("api", path, with_="<html>502 Bad Gateway</html>")

    assert preflight.main() == 0
    assert "burst=0 PRs, 0 issues" in capsys.readouterr().out


def test_unreadable_events_refuse_the_run_rather_than_approving_it(
    gh: FakeGh,
) -> None:
    """Failing to read the events leaves approvals at zero.

    That refuses the run, which is the safe direction for a check whose job is
    to stop things — the opposite would let a blip wave a runaway through.
    """
    _posts(gh, today=16, past=15)
    _pause_issue(gh, _closed("maintainer"))
    gh.respond(
        "api",
        "--paginate",
        "--slurp",
        f"repos/owner/repo/issues/{NEW_ISSUE}/events?per_page=100",
        with_=1,
    )

    assert preflight.main() == 1


def test_a_label_that_cannot_be_created_does_not_stop_the_filing(
    gh: FakeGh,
) -> None:
    """The label already exists on every repo after the first incident, and
    `gh label create` has no idempotent form — so its failure is expected."""
    _posts(gh, today=16, past=15)
    gh.respond("label", "create", with_=1)

    assert preflight.main() == 1
    assert gh.called("issue", "create"), gh.calls


def test_keeps_its_own_record_when_it_cannot_vouch_for_a_sibling(
    gh: FakeGh,
) -> None:
    """Unable to read its own identity, the reconciler defers to nobody.

    The probe's author check is what stops a label on somebody else's issue
    being adopted as the keeper — and on this record a close is read as an
    approval. Without a name to check against, keeping both open is the safe
    failure.
    """
    _posts(gh, today=16, past=15)
    _probe(gh, 41)
    identities = iter([_serve({"login": BOT, "id": BOT_ID}), None])
    gh.respond("api", "user", with_=lambda argv, stdin: next(identities) or _serve(1))

    assert preflight.main() == 1
    assert not gh.called("issue", "close"), (
        f"stood down to a sibling it could not vouch for: {gh.calls}"
    )
