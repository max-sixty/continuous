"""Tests for the persisted evidence lifecycles used by scheduled skills."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "tend-ci-runner" / "scripts"
sys.path.insert(0, str(SCRIPTS))
review_runs = importlib.import_module("review_runs")
review_reviewers = importlib.import_module("review_reviewers")
NOW = datetime(2026, 9, 6, tzinfo=UTC)


def test_review_runs_prepare_persists_the_current_tracker_and_closes_stale_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setenv("REVIEW_RUNS_STATE", str(state))
    calls: list[tuple[str, ...]] = []
    issues = [
        {"number": 5, "title": "review-runs-tracking: 2026-09", "state": "OPEN"},
        {"number": 4, "title": "review-runs-tracking: 2026-08", "state": "OPEN"},
        {"number": 3, "title": "review-runs-tracking: 2026-08", "state": "CLOSED"},
    ]

    def json_call(*args: str) -> object:
        if args[:2] == ("issue", "list"):
            return issues
        if args[:2] == ("issue", "view"):
            return {
                "comments": [{"author": {"login": "bot"}, "body": f"history-{args[2]}"}]
            }
        raise AssertionError(args)

    monkeypatch.setattr(review_runs.github_cli, "json_call", json_call)
    monkeypatch.setattr(
        review_runs.github_cli,
        "run",
        lambda *args, **kwargs: calls.append(args) or "",
    )

    assert review_runs.prepare(now=NOW) == 0

    assert json.loads(state.read_text()) == {
        "tracking_number": 5,
        "month": "2026-09",
    }
    assert json.loads(capsys.readouterr().out) == {
        "tracking_number": 5,
        "month": "2026-09",
        "current_comments": [{"author": "bot", "body": "history-5"}],
        "previous_comments": [{"author": "bot", "body": "history-3"}],
    }
    assert any(call[:3] == ("issue", "close", "4") for call in calls)


def test_review_runs_prepare_creates_the_monthly_tracker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setenv("REVIEW_RUNS_STATE", str(state))
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def json_call(*args: str) -> object:
        if args[:2] == ("issue", "list"):
            return []
        if args[:2] == ("issue", "view"):
            return {"comments": []}
        raise AssertionError(args)

    def run(*args: str, input: str | None = None) -> str:
        calls.append((args, input))
        if args[:2] == ("issue", "create"):
            return "https://github.com/owner/repo/issues/12\n"
        return ""

    monkeypatch.setattr(review_runs.github_cli, "json_call", json_call)
    monkeypatch.setattr(review_runs.github_cli, "run", run)

    assert review_runs.prepare(now=NOW) == 0

    assert json.loads(state.read_text())["tracking_number"] == 12
    assert json.loads(capsys.readouterr().out)["current_comments"] == []
    create = next(args for args, _ in calls if args[:2] == ("issue", "create"))
    assert "review-runs-tracking: 2026-09" in create
    assert review_runs.BODY in create


def test_review_runs_append_updates_the_latest_evidence_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.json"
    findings = tmp_path / "findings.md"
    state.write_text(json.dumps({"tracking_number": 5, "month": "2026-09"}))
    findings.write_text("\n## Run 42\nnew evidence\n")
    monkeypatch.setenv("REVIEW_RUNS_STATE", str(state))
    monkeypatch.setenv("REVIEW_RUNS_FINDINGS", str(findings))
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setattr(review_runs.github_cli, "repository", lambda: "owner/repo")
    monkeypatch.setattr(
        review_runs.github_cli, "json_call", lambda *args: {"login": "bot"}
    )
    monkeypatch.setattr(
        review_runs.github_cli,
        "paginated",
        lambda *args: [
            {"id": 7, "user": {"login": "bot"}, "body": "\n## Run 41\nold\n"},
            {"id": 8, "user": {"login": "human"}, "body": "\n## Run 1\nnoise\n"},
        ],
    )
    writes: list[tuple[tuple[str, ...], str | None]] = []
    monkeypatch.setattr(
        review_runs.github_cli,
        "run",
        lambda *args, input=None: writes.append((args, input)) or "",
    )

    assert review_runs.append() == 0

    args, body = writes[0]
    assert "repos/owner/repo/issues/comments/7" in args
    assert (
        json.loads(body or "")["body"]
        == "\n## Run 41\nold\n\n## Run 42\nnew evidence\n"
    )
    assert json.loads(capsys.readouterr().out)["action"] == "appended"


def test_review_reviewers_prepare_persists_gist_and_returns_both_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setenv("REVIEW_REVIEWERS_STATE", str(state))
    current = {
        "id": "current",
        "html_url": "https://gist.github.com/current",
        "description": "review-reviewers evidence: owner/target 2026-09",
    }
    previous = {
        "id": "previous",
        "html_url": "https://gist.github.com/previous",
        "description": "review-reviewers evidence: owner/target 2026-08",
    }
    monkeypatch.setattr(
        review_reviewers.github_cli,
        "json_call",
        lambda *args, **kwargs: [
            {"number": 9, "title": "review-reviewers-tracking: 2026-09"}
        ],
    )
    monkeypatch.setattr(
        review_reviewers.github_cli, "paginated", lambda *args: [current, previous]
    )
    monkeypatch.setattr(
        review_reviewers,
        "_gist_content",
        lambda gist_id: {"current": "now", "previous": "before"}[gist_id],
    )

    assert review_reviewers.prepare("owner/target", now=NOW) == 0

    output = json.loads(capsys.readouterr().out)
    assert json.loads(state.read_text()) == {
        "target": "owner/target",
        "month": "2026-09",
        "tracking_number": 9,
        "gist_id": "current",
        "gist_url": "https://gist.github.com/current",
    }
    assert output["current_evidence"] == "now"
    assert output["previous_evidence"] == "before"


def test_review_reviewers_prepare_creates_and_indexes_a_secret_gist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setenv("REVIEW_REVIEWERS_STATE", str(state))
    previous = {
        "id": "previous",
        "description": "review-reviewers evidence: owner/target 2026-08",
    }
    api_inputs: list[tuple[tuple[str, ...], str | None]] = []
    run_calls: list[tuple[tuple[str, ...], str | None]] = []

    def json_call(*args: str, input: str | None = None, **_: object) -> object:
        api_inputs.append((args, input))
        if args[:2] == ("issue", "list"):
            return []
        if args[:2] == ("api", "gists"):
            return {
                "id": "current",
                "html_url": "https://gist.github.com/current",
            }
        raise AssertionError(args)

    def run(*args: str, input: str | None = None) -> str:
        run_calls.append((args, input))
        if args[:2] == ("issue", "create"):
            return "https://github.com/owner/repo/issues/9\n"
        return ""

    monkeypatch.setattr(review_reviewers.github_cli, "json_call", json_call)
    monkeypatch.setattr(
        review_reviewers.github_cli, "paginated", lambda *args: [previous]
    )
    monkeypatch.setattr(review_reviewers.github_cli, "run", run)
    monkeypatch.setattr(review_reviewers.github_cli, "repository", lambda: "owner/repo")
    monkeypatch.setattr(
        review_reviewers,
        "_gist_content",
        lambda gist_id: {"current": "now", "previous": "before"}[gist_id],
    )

    assert review_reviewers.prepare("owner/target", now=NOW) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["tracking_number"] == 9
    assert output["gist_id"] == "current"
    _, create_input = next(
        call for call in api_inputs if call[0][:2] == ("api", "gists")
    )
    assert json.loads(create_input or "") == {
        "description": "review-reviewers evidence: owner/target 2026-09",
        "public": False,
        "files": {
            "findings.md": {
                "content": (
                    "# review-reviewers evidence — owner/target — 2026-09\n\n"
                    "Secret gist. Append-only log of below-threshold findings used "
                    "for gate evaluation.\n"
                )
            }
        },
    }
    _, announce_input = next(
        call
        for call in run_calls
        if call[0][:2] == ("api", "repos/owner/repo/issues/9/comments")
    )
    assert "https://gist.github.com/current" in json.loads(announce_input or "")["body"]


def test_review_reviewers_append_refetches_before_patching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state.json"
    findings = tmp_path / "findings.md"
    state.write_text(
        json.dumps({"gist_id": "current", "gist_url": "https://gist/current"})
    )
    findings.write_text("\n## Run 42\nnew\n")
    monkeypatch.setenv("REVIEW_REVIEWERS_STATE", str(state))
    monkeypatch.setenv("REVIEW_REVIEWERS_FINDINGS", str(findings))
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setattr(review_reviewers, "_gist_content", lambda _: "old\n")
    writes: list[str] = []
    monkeypatch.setattr(
        review_reviewers.github_cli,
        "run",
        lambda *args, input=None: writes.append(input or "") or "",
    )

    assert review_reviewers.append() == 0

    assert json.loads(writes[0]) == {
        "files": {"findings.md": {"content": "old\n\n## Run 42\nnew\n"}}
    }
    assert json.loads(capsys.readouterr().out)["action"] == "appended"
