from __future__ import annotations

import json
from pathlib import Path

import mark_notification_read
import pytest
from _fakes import FakeGh

REPO = "owner/repo"
RUN_ID = "12345"
RUN_STARTED_AT = "2026-01-02T00:00:00Z"
SETTLED = "2026-01-01T00:00:00Z"
MID_RUN = "2026-03-01T00:00:00Z"
ISSUE_URL = f"https://api.github.com/repos/{REPO}/issues/7"


@pytest.fixture
def event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Actions env for an `issues` event on issue 7 of owner/repo."""
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"issue": {"number": 7}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", RUN_ID)
    monkeypatch.setenv("GITHUB_WORKFLOW", "tend-triage")
    return path


def _run_metadata(fake_gh: FakeGh, started_at: object) -> None:
    """Answer the run-metadata fetch; an ``int`` makes the call fail."""
    body = started_at if isinstance(started_at, int) else {"run_started_at": started_at}
    fake_gh.respond("api", f"repos/{REPO}/actions/runs/{RUN_ID}", with_=body)


def _inbox(fake_gh: FakeGh, *threads: tuple[str, str, str]) -> None:
    """Serve an inbox of ``(id, subject_url, updated_at)`` and accept PATCHes."""
    fake_gh.respond(
        "api",
        "notifications",
        with_=[
            {"id": tid, "updated_at": updated_at, "subject": {"url": url}}
            for tid, url, updated_at in threads
        ],
    )
    for tid, _, _ in threads:
        fake_gh.respond("api", f"notifications/threads/{tid}", with_="")


def _patch_calls(fake_gh: FakeGh) -> list[str]:
    """The thread ids the step asked GitHub to mark read."""
    return [
        call[1].removeprefix("notifications/threads/")
        for call in fake_gh.calls
        if call[1].startswith("notifications/threads/")
    ]


@pytest.mark.parametrize(
    ("event_name", "payload", "expected"),
    [
        pytest.param(
            "pull_request_target",
            {"pull_request": {"number": 4}},
            f"https://api.github.com/repos/{REPO}/pulls/4",
            id="pull-request-event",
        ),
        pytest.param(
            "issues",
            {"issue": {"number": 7}},
            ISSUE_URL,
            id="issue",
        ),
        pytest.param(
            "issue_comment",
            {"issue": {"number": 7}},
            ISSUE_URL,
            id="comment-on-an-issue",
        ),
        pytest.param(
            "issue_comment",
            {
                "issue": {
                    "number": 4,
                    "pull_request": {
                        "url": f"https://api.github.com/repos/{REPO}/pulls/4"
                    },
                }
            },
            f"https://api.github.com/repos/{REPO}/pulls/4",
            id="comment-on-a-pr",
        ),
        pytest.param(
            "repository_dispatch",
            {"action": "tend-review", "client_payload": {"pr_number": 4}},
            None,
            id="review-recovery",
        ),
        pytest.param(
            "repository_dispatch",
            {"action": "something-else", "client_payload": {"pr_number": 4}},
            None,
            id="unrelated-dispatch",
        ),
        pytest.param(
            "repository_dispatch",
            {"action": "tend-review", "client_payload": {"pr_number": "4"}},
            None,
            id="malformed-review-recovery",
        ),
        pytest.param("schedule", {}, None, id="nothing-to-mark"),
    ],
)
def test_subject_url_names_the_thread_the_event_belongs_to(
    event_name: str, payload: dict[str, object], expected: str | None
) -> None:
    """`issue_comment` fires for issues and PR conversations alike.

    A PR notification's subject is always `/pulls/N`, so the issue's
    `pull_request` field is what decides which URL the inbox is searched for —
    an `/issues/N` guess would never match and would leave the thread unread.
    """
    assert mark_notification_read.subject_url(REPO, event_name, payload) == expected


def test_marks_a_thread_whose_activity_predates_the_run(
    event: Path, fake_gh: FakeGh
) -> None:
    _run_metadata(fake_gh, RUN_STARTED_AT)
    _inbox(fake_gh, ("999", ISSUE_URL, SETTLED))

    assert mark_notification_read.main() == 0
    assert ("api", "notifications/threads/999", "-X", "PATCH") in fake_gh.calls


def test_successful_review_dispatch_leaves_conversation_for_notifications_poll(
    event: Path,
    fake_gh: FakeGh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event.write_text(
        json.dumps({"action": "tend-review", "client_payload": {"pr_number": 4}})
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "repository_dispatch")
    monkeypatch.setenv("GITHUB_WORKFLOW", "tend-review")
    assert mark_notification_read.main() == 0
    assert fake_gh.calls == []


def test_deferred_review_dispatch_leaves_its_pr_notification_unread(
    event: Path,
    fake_gh: FakeGh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event.write_text(
        json.dumps({"action": "tend-review", "client_payload": {"pr_number": 4}})
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "repository_dispatch")
    monkeypatch.setenv("GITHUB_WORKFLOW", "tend-review")
    assert mark_notification_read.main() == 0
    assert fake_gh.calls == []


def test_native_review_leaves_conversation_for_notifications_poll(
    event: Path,
    fake_gh: FakeGh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event.write_text(json.dumps({"pull_request": {"number": 4}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_WORKFLOW", "custom-review-name")

    assert mark_notification_read.main() == 0
    assert fake_gh.calls == []


def test_leaves_activity_newer_than_the_run(event: Path, fake_gh: FakeGh) -> None:
    """Mid-run activity is what the next workflow run has to see."""
    _run_metadata(fake_gh, RUN_STARTED_AT)
    _inbox(fake_gh, ("999", ISSUE_URL, MID_RUN))

    assert mark_notification_read.main() == 0
    assert _patch_calls(fake_gh) == []


def test_leaves_a_thread_for_another_subject(event: Path, fake_gh: FakeGh) -> None:
    """Only the triggering event's own thread is this run's to clear."""
    _run_metadata(fake_gh, RUN_STARTED_AT)
    _inbox(
        fake_gh,
        ("999", ISSUE_URL, SETTLED),
        ("998", f"https://api.github.com/repos/{REPO}/pulls/7", SETTLED),
        ("997", "https://api.github.com/repos/other/repo/issues/7", SETTLED),
    )

    assert mark_notification_read.main() == 0
    assert _patch_calls(fake_gh) == ["999"]


def test_tolerates_a_run_metadata_failure(
    event: Path, fake_gh: FakeGh, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transient failure fetching `run_started_at` must not fail the step.

    Both harness actions gate this step on `if: success()`, so a non-zero exit
    here turns a fully-successful agent run red. Without the timestamp the
    `updated_at <= started` guard cannot be evaluated, so nothing is marked.
    """
    _run_metadata(fake_gh, 1)
    _inbox(fake_gh, ("999", ISSUE_URL, SETTLED))

    assert mark_notification_read.main() == 0
    assert _patch_calls(fake_gh) == []
    assert "::warning::Could not read run_started_at" in capsys.readouterr().out


def test_marks_nothing_for_an_event_whose_shape_it_cannot_read(
    event: Path, fake_gh: FakeGh
) -> None:
    """A payload that names no number is nothing to mark, not a failure.

    Both harness actions gate this step on `if: success()`, so raising on a
    shape that arrived without `.issue.number` would turn a fully-successful
    agent run red at its last step.
    """
    event.write_text(json.dumps({"issue": None}))

    assert mark_notification_read.main() == 0
    assert fake_gh.calls == []


def test_leaves_a_notification_it_cannot_read_as_a_dated_thread(
    event: Path, fake_gh: FakeGh
) -> None:
    """An entry missing its stamp, its subject or its id is skipped, not read into.

    A missing stamp is an unknown age, which is the same reason a run with no
    `run_started_at` marks nothing at all: marking it anyway would swallow the
    mid-run activity the guard exists to preserve.
    """
    _run_metadata(fake_gh, RUN_STARTED_AT)
    fake_gh.respond(
        "api",
        "notifications",
        with_=[
            {"id": "996", "subject": {"url": ISSUE_URL}},
            {"id": "997", "updated_at": SETTLED},
            {"id": "995", "subject": None, "updated_at": SETTLED},
            "not a notification at all",
            {"subject": {"url": ISSUE_URL}, "updated_at": SETTLED},
            {"id": "999", "subject": {"url": ISSUE_URL}, "updated_at": SETTLED},
        ],
    )
    fake_gh.respond("api", "notifications/threads/999", with_="")

    assert mark_notification_read.main() == 0
    assert _patch_calls(fake_gh) == ["999"]


def test_marks_nothing_for_an_event_that_names_no_thread(
    event: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    assert mark_notification_read.main() == 0
    assert fake_gh.calls == []


@pytest.mark.parametrize(
    "inbox", [1, "<html>rate limited</html>", {"message": "Bad credentials"}]
)
def test_tolerates_an_inbox_it_cannot_read(
    event: Path, fake_gh: FakeGh, capsys: pytest.CaptureFixture[str], inbox: object
) -> None:
    """A failed request, an HTML 200, and an error object are all non-fatal."""
    _run_metadata(fake_gh, RUN_STARTED_AT)
    fake_gh.respond("api", "notifications", with_=inbox)

    assert mark_notification_read.main() == 0
    assert "::warning::Failed to mark notification as read" in capsys.readouterr().out


def test_a_failed_patch_leaves_the_step_green(event: Path, fake_gh: FakeGh) -> None:
    """One thread that will not mark must not fail the step or strand the rest."""
    _run_metadata(fake_gh, RUN_STARTED_AT)
    _inbox(fake_gh, ("998", ISSUE_URL, SETTLED), ("999", ISSUE_URL, SETTLED))
    fake_gh.respond("api", "notifications/threads/998", with_=1)

    assert mark_notification_read.main() == 0
    assert _patch_calls(fake_gh) == ["998", "999"]


def test_refuses_to_run_without_the_actions_env(
    event: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "")
    with pytest.raises(SystemExit, match="GITHUB_RUN_ID"):
        mark_notification_read.main()
    assert fake_gh.calls == []
