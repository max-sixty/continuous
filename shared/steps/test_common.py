from __future__ import annotations

import json
import subprocess
from pathlib import Path

import _common
import pytest
from _fakes import FakeGh, GithubFiles


def test_require_env_names_the_unset_and_the_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEND_A", "set")
    monkeypatch.setenv("TEND_B", "")
    monkeypatch.delenv("TEND_C", raising=False)
    with pytest.raises(SystemExit, match="TEND_B, TEND_C"):
        _common.require_env("TEND_A", "TEND_B", "TEND_C")
    assert _common.require_env("TEND_A") == {"TEND_A": "set"}


def test_read_ndjson_skips_a_torn_last_line(tmp_path: Path) -> None:
    path = tmp_path / "stream.json"
    path.write_text('{"type": "a"}\n\n{"type": "b"}\n{"type": "c", "trunc')
    assert [e["type"] for e in _common.read_ndjson(path)] == ["a", "b"]


def test_set_output_uses_the_heredoc_form_for_multiline(
    github_files: GithubFiles,
) -> None:
    _common.set_output("one", "x")
    _common.set_output("two", "a\nb")
    assert github_files.outputs() == {"one": "x", "two": "a\nb"}


def test_stop_commands_brackets_untrusted_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _common.stop_commands():
        print("::error::forged")
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("::stop-commands::tend-")
    token = out[0].removeprefix("::stop-commands::")
    assert out[1] == "::error::forged"
    assert out[2] == f"::{token}::"


def test_annotate_keeps_the_message_on_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The runner ends a line on a bare `\\r` as well as on `\\n`."""
    _common.annotate("error", "first\n::warning::second")
    _common.annotate("error", "first\r::warning::second")
    assert capsys.readouterr().out == (
        "::error::first ::warning::second\n::error::first ::warning::second\n"
    )


def test_append_summary_survives_an_unencodable_surrogate(
    github_files: GithubFiles,
) -> None:
    """A `\\udXXX` escape in the agent's text decodes to a lone surrogate UTF-8 cannot encode."""
    _common.append_summary("before \ud800 after")
    assert github_files.summary.read_text(encoding="utf-8") == "before ? after\n"


def test_fake_gh_serves_the_longest_prefix_and_records(fake_gh: FakeGh) -> None:
    fake_gh.respond("api", with_={"a": 1})
    fake_gh.respond("api", "repos/x", with_="plain")
    fake_gh.respond("pr", "list", with_=1)
    assert _common.gh_json("api", "user") == {"a": 1}
    assert _common.gh("api", "repos/x", "--jq", ".") == "plain"
    with pytest.raises(subprocess.CalledProcessError):
        _common.gh("pr", "list")
    with pytest.raises(AssertionError, match="unexpected gh call: issue view 1"):
        _common.gh("issue", "view", "1")
    assert fake_gh.called("api") == [("api", "user"), ("api", "repos/x", "--jq", ".")]


def test_gh_relays_stderr_before_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gh's own explanation reaches the step log whether or not the caller tolerates the failure."""

    def failing(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 1, "", "gh: Bad credentials (HTTP 401)\n"
        )

    monkeypatch.setattr(_common.subprocess, "run", failing)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        _common.gh("api", "user")
    assert caught.value.stderr == "gh: Bad credentials (HTTP 401)\n"
    assert capsys.readouterr().err == "gh: Bad credentials (HTTP 401)\n"


def test_run_turns_an_unhandled_gh_failure_into_one_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def main() -> int:
        raise subprocess.CalledProcessError(
            4, ["gh", "api", "repos/x"], "", "Not Found"
        )

    with pytest.raises(SystemExit) as caught:
        _common.run(main)
    assert caught.value.code == 1
    assert capsys.readouterr().out == "::error::gh api repos/x failed (exit 4)\n"


def test_subject_number_is_an_int_whatever_the_payload_calls_it(
    monkeypatch: pytest.MonkeyPatch, actions_env: Path
) -> None:
    """A relayed review's PR number arrives as a form string, not a JSON int.

    Records are grouped by this key, and `"99"` and `99` are different keys.
    """
    assert _common.subject_number() == 851
    actions_env.write_text(json.dumps({"client_payload": {"pr": "99"}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "repository_dispatch")
    assert _common.subject_number() == 99


@pytest.mark.parametrize(
    ("event", "payload", "expected"),
    [
        # No thread of its own, a payload that isn't the shape its event
        # promises, and a number that isn't one.
        ("schedule", {}, None),
        ("issues", {"issue": None}, None),
        ("repository_dispatch", {"client_payload": {"pr": ""}}, None),
        # A dispatch is a POST anyone with `contents: write` can shape, and
        # `_issue.ref()` renders the result into a public issue comment. A
        # coerced `1` or `3` is a plausible reference to somebody else's PR.
        ("repository_dispatch", {"client_payload": {"pr": True}}, None),
        ("repository_dispatch", {"client_payload": {"pr": 3.9}}, None),
    ],
)
def test_subject_number_is_none_when_the_event_names_no_thread(
    monkeypatch: pytest.MonkeyPatch,
    actions_env: Path,
    event: str,
    payload: dict,
    expected: int | None,
) -> None:
    actions_env.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    assert _common.subject_number() is expected


@pytest.mark.parametrize(
    ("event", "payload", "expected"),
    [
        # The events that carry their own commit, for which GITHUB_SHA is the
        # default branch's tip rather than the PR head the workflow checks out
        # or the run the ci-fix job was dispatched to fix.
        ("pull_request_target", {"pull_request": {"head": {"sha": "head0"}}}, "head0"),
        ("workflow_run", {"workflow_run": {"head_sha": "failed0"}}, "failed0"),
        # An event whose subject is a thread reports no commit. GITHUB_SHA is
        # the default branch's tip, and a mention on a PR `gh pr checkout`s the
        # PR head straight after, so recording it would name a commit the run
        # never touched — and disagree with the review record for the same
        # revision.
        ("issues", {"issue": {"number": 7}}, None),
        ("issue_comment", {"issue": {"number": 7}}, None),
        ("repository_dispatch", {"client_payload": {"pr": 99}}, None),
        # A pull-request payload that doesn't carry the head takes the same
        # `None` rather than falling back to the base commit.
        ("pull_request_target", {"pull_request": {}}, None),
        # Nothing names a thread here, so GITHUB_SHA is the run's own ref.
        ("schedule", {}, "checkout0"),
        ("workflow_run", {"workflow_run": {"head_sha": ""}}, "checkout0"),
    ],
)
def test_subject_sha_reports_only_a_commit_the_event_is_about(
    monkeypatch: pytest.MonkeyPatch,
    actions_env: Path,
    event: str,
    payload: dict,
    expected: str | None,
) -> None:
    actions_env.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_SHA", "checkout0")
    assert _common.subject_sha() == expected


def test_event_payload_survives_a_file_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, actions_env: Path
) -> None:
    """The payload annotates work already under way; it may not cost it.

    A GitHub blip can leave an HTML error page where the JSON should be, and
    a step body reading this outside Actions has no GITHUB_EVENT_PATH at all.
    """
    actions_env.write_text("<html>not an event payload</html>")
    assert _common.event_payload() == {}

    actions_env.unlink()
    assert _common.event_payload() == {}

    monkeypatch.delenv("GITHUB_EVENT_PATH")
    assert _common.event_payload() == {}
    assert _common.subject_number() is None
