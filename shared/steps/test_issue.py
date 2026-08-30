from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import _common
import _issue
import pytest
from _fakes import FakeGh

RUN_LINK = "[workflow run](https://github.com/owner/repo/actions/runs/12345)"

OUTAGE_TITLE = "Bot temporarily unavailable"
OUTAGE_LABEL = "tend-outage"


def _trigger(
    monkeypatch: pytest.MonkeyPatch, event: Path, name: str, payload: dict
) -> str:
    event.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", name)
    return _issue.ref()


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        ("pull_request_target", {"pull_request": {"number": 851}}, "#851"),
        ("pull_request_review", {"pull_request": {"number": 12}}, "#12"),
        ("pull_request_review_comment", {"pull_request": {"number": 12}}, "#12"),
        ("issues", {"issue": {"number": 7}}, "#7"),
        ("issue_comment", {"issue": {"number": 7}}, "#7"),
        # tend-mention relays review events through a secretless job that
        # re-posts them as a repository_dispatch, so the PR number arrives in
        # the payload rather than in a `pull_request` object.
        ("repository_dispatch", {"client_payload": {"pr": 99}}, "#99"),
        (
            "workflow_run",
            {"workflow_run": {"id": 555}},
            "CI fix for [run 555](https://github.com/owner/repo/actions/runs/555)",
        ),
        ("workflow_run", {}, "CI fix for workflow run"),
        # An event with no thread of its own, and payloads whose shape is not
        # the one the event promises.
        ("schedule", {}, ""),
        ("issues", {"issue": None}, ""),
        ("pull_request_target", {"pull_request": {}}, ""),
    ],
)
def test_ref_names_the_thread_a_stranded_run_came_from(
    monkeypatch: pytest.MonkeyPatch,
    actions_env: Path,
    name: str,
    payload: dict,
    expected: str,
) -> None:
    """The Trigger cell is the only pointer back to the work a run stranded."""
    assert _trigger(monkeypatch, actions_env, name, payload) == expected


def test_row_stamps_the_time_and_links_this_run(
    monkeypatch: pytest.MonkeyPatch, actions_env: Path
) -> None:
    """One format wherever a row lands, so a seed body and a comment match."""
    monkeypatch.setattr(
        _common, "utcnow", lambda: datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    )
    assert _issue.row() == (
        "| When | Run | Trigger |\n"
        "|------|-----|---------|\n"
        f"| 2026-01-02T12:00:00Z | {RUN_LINK} | #851 |"
    )


def test_row_reads_n_a_when_the_trigger_has_no_thread(
    monkeypatch: pytest.MonkeyPatch, actions_env: Path
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    actions_env.write_text("{}")
    assert _issue.row().endswith(f"| {RUN_LINK} | N/A |")


def test_row_reads_n_a_when_the_event_payload_cannot_be_read(
    actions_env: Path,
) -> None:
    """The payload fills one cell; the row is the whole record of an incident.

    Both callers reach this only once a run has failed or been refused, so a
    file torn mid-write, or one the step cannot open at all, has to cost that
    cell its `N/A` rather than cost the incident its record.
    """
    actions_env.write_text("<html>not an event payload</html>")
    assert _issue.row().endswith(f"| {RUN_LINK} | N/A |")

    actions_env.unlink()
    assert _issue.row().endswith(f"| {RUN_LINK} | N/A |")


def test_matching_keeps_this_records_title_lowest_first(fake_gh: FakeGh) -> None:
    """The label alone pins neither author nor title, and the bot can label
    anything — so all three constraints run, and the order is by number."""
    fake_gh.respond(
        "issue",
        "list",
        with_=[
            {"number": 42, "title": OUTAGE_TITLE},
            {"number": 8, "title": OUTAGE_TITLE},
            {"number": 3, "title": "Something a maintainer labelled"},
        ],
    )

    assert _issue.matching(OUTAGE_LABEL, "open", OUTAGE_TITLE) == [8, 42]
    assert _issue.canonical(OUTAGE_LABEL, "open", OUTAGE_TITLE) == 8
    call = fake_gh.called("issue", "list")[0]
    assert call[call.index("--author") + 1] == "@me"
    assert call[call.index("--label") + 1] == OUTAGE_LABEL
    assert call[call.index("--limit") + 1] == "100"


def test_canonical_raises_rather_than_reading_a_failed_list_as_none(
    fake_gh: FakeGh,
) -> None:
    """ "The read failed" and "there is none" are different facts.

    A caller that conflates them files a fresh record while one is already
    open, and the downward probe will not merge that duplicate away.
    """
    fake_gh.respond("issue", "list", with_=1)
    with pytest.raises(subprocess.CalledProcessError):
        _issue.canonical(OUTAGE_LABEL, "open", OUTAGE_TITLE)


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        ({}, True),
        ({"state": "closed"}, False),
        ({"title": "A maintainer's issue"}, False),
        ({"user": {"login": "someone"}}, False),
        ({"labels": [{"name": "unrelated-label"}]}, False),
    ],
)
def test_is_ours_takes_all_of_author_title_label_and_open(
    issue: dict, expected: bool
) -> None:
    """Author above all: the bot holds `issues: write`, so without it a label
    put on somebody else's issue could be adopted as the keeper — and on the
    rate-limit record a close on that issue is read as an approval."""
    candidate = {
        "number": 41,
        "state": "open",
        "title": OUTAGE_TITLE,
        "user": {"login": "tend-agent"},
        "labels": [{"name": OUTAGE_LABEL}],
        **issue,
    }
    assert (
        _issue.is_ours(
            candidate, title=OUTAGE_TITLE, label=OUTAGE_LABEL, login="tend-agent"
        )
        is expected
    )


def test_recorded_text_reads_as_not_recorded_when_the_read_fails(
    fake_gh: FakeGh,
) -> None:
    """A duplicate row is the cheaper loss, so a failed read falls through."""
    fake_gh.respond("issue", "view", with_=1)
    assert _issue.recorded_text(41) == ""
    fake_gh.respond(
        "issue", "view", with_={"body": "seed", "comments": [{"body": "a row"}]}
    )
    assert _issue.recorded_text(41) == "seed\na row"
