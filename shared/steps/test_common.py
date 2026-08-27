from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import _common
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
