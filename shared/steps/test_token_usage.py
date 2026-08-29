"""Tests for the token-usage step body.

The Claude fixtures mirror the shapes observed in real uploaded artifacts, and
``output_tokens == 4500`` is what discriminates between the three accounting
paths: the stream-json's message-start placeholders sum to 12, and the `Task`
subagent's transcript beside the session would add 7000.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import token_usage
from _fakes import GithubFiles

# Final per-message usage, as the session JSONL records it.
FINAL_USAGE = [
    {
        "input_tokens": 10,
        "output_tokens": 3000,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 20000,
    },
    {
        "input_tokens": 5,
        "output_tokens": 1500,
        "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 40000,
    },
]
# The same two messages as the stream-json emits them: input/cache identical,
# output still at its message-start placeholder.
STREAM_USAGE = [dict(usage, output_tokens=6) for usage in FINAL_USAGE]

# A `Task` subagent's own transcript, which real artifacts carry alongside the
# session it belongs to. Its usage is not in the `result` event, so nothing
# here may reach the totals.
SUBAGENT_USAGE = {
    "input_tokens": 300,
    "output_tokens": 7000,
    "cache_creation_input_tokens": 40000,
    "cache_read_input_tokens": 900000,
}

TRUNCATED = '{"type":"assistant","mess'

# What `run_context` reads. A test that asserts an absent context has to strip
# them, because pytest itself runs under Actions in CI.
CONTEXT_VARS = (
    "GITHUB_REPOSITORY",
    "GITHUB_WORKFLOW",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_EVENT_NAME",
    "GITHUB_SHA",
    "GITHUB_EVENT_PATH",
)


def _assistant(msg_id: str, usage: dict[str, int], *, final: bool) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "stop_reason": "end_turn" if final else None,
            "usage": usage,
        },
    }


def _ndjson(path: Path, lines: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _session_jsonl(logs_dir: Path) -> Path:
    """A cancelled session's JSONL: real usage, each message duplicated.

    Writes the subagent transcript beside it too — ``<session>/subagents/`` is
    how Claude Code lays a ``Task`` out on disk, and the consolidating
    ``cp -a .../projects/.`` copies the subtree into the log dir.
    """
    project = logs_dir / "-home-runner-work-repo-repo"
    lines: list[dict[str, object]] = [{"type": "user"}]
    for i, usage in enumerate(FINAL_USAGE):
        entry = _assistant(f"msg_{i}", usage, final=True)
        lines += [entry, dict(entry), {"type": "user"}]
    lines.append({"type": "user"})

    _ndjson(
        project / "session" / "subagents" / "agent-a1b2c3.jsonl",
        [
            {"type": "user"},
            _assistant("msg_sub", SUBAGENT_USAGE, final=True),
            {"type": "user"},
        ],
    )
    return _ndjson(project / "session.jsonl", lines)


def _cancelled_stream(tmp_path: Path) -> Path:
    """Stream-json for the same session: assistant events, no `result`."""
    lines: list[dict[str, object]] = [{"type": "system"}]
    for i, usage in enumerate(STREAM_USAGE):
        entry = _assistant(f"msg_{i}", usage, final=False)
        lines += [entry, dict(entry), {"type": "user"}]
    return _ndjson(tmp_path / "stream.json", lines)


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    path = tmp_path / "logs"
    path.mkdir()
    return path


def test_reconstructs_a_cancelled_session(tmp_path: Path, logs_dir: Path) -> None:
    """A cancelled session must be accounted from its session JSONL.

    `tend-review` runs with `cancel-in-progress: true`, so cancellation is
    routine — and a cancelled session never emits a `type: "result"` event.
    The step is `if: always()`, so it still writes token-usage.json and still
    uploads the artifact; only the accounting would be lost. Reporting zeros
    for a run that did real work (and may already have posted a review) biases
    every downstream total by the cancellation rate.

    The totals below also rule out the two ways of reaching a plausible-looking
    wrong number: summing the stream-json's non-final assistant events, whose
    message-start `output_tokens` come to 12, and slurping the `Task`
    subagent's transcript alongside the main loop the `result` event counts.
    """
    _session_jsonl(logs_dir)

    usage = token_usage.claude_usage(
        stream=_cancelled_stream(tmp_path), logs_dir=logs_dir, model="opus"
    )

    assert usage["output_tokens"] == 4500, (
        f"cancelled session reported output_tokens={usage['output_tokens']}; "
        "the session JSONL records 4500 across two messages — 12 means the "
        "stream-json's placeholders were summed, 11500 the subagent's"
    )
    assert usage["input_tokens"] == 15
    assert usage["cache_creation_input_tokens"] == 1500
    assert usage["cache_read_input_tokens"] == 60000, (
        "60000 is the main loop's cache read; the subagent would add 900000"
    )
    # Four `user` lines bracket the two assistant turns; the one that opens the
    # session is the prompt, not a turn.
    assert usage["turns"] == 3, "the subagent's `user` lines are not turns"
    assert usage["partial"] is True, (
        "a reconstructed total must be distinguishable from a run that "
        "genuinely cost nothing"
    )
    assert usage["cost_usd"] is None, (
        "only the result event carries cost; a 0 here would repeat the bug "
        "this fallback exists to fix"
    )


def test_sums_every_result_event_but_takes_cost_from_the_last() -> None:
    """A `run_in_background: true` Bash makes the session emit a second result.

    `usage.*` and `num_turns` are per-event while `total_cost_usd` is
    cumulative, so summing cost alongside the rest would double-count the
    first leg.
    """
    usage = token_usage.result_usage(
        [
            {
                "type": "result",
                "num_turns": 3,
                "total_cost_usd": 0.5,
                "usage": {"input_tokens": 10, "output_tokens": 100},
            },
            {
                "type": "result",
                "num_turns": 2,
                "total_cost_usd": 0.9,
                "usage": {"input_tokens": 5, "output_tokens": 50},
            },
        ],
        "opus",
    )

    assert usage == {
        "input_tokens": 15,
        "output_tokens": 150,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "turns": 5,
        "model": "opus",
        "cost_usd": 0.9,
        "partial": False,
    }


def test_survives_a_truncated_line_beside_a_second_session(
    tmp_path: Path, logs_dir: Path
) -> None:
    """A truncated file must not take the next file's first line with it.

    The files are read one at a time; a reader that concatenated them would
    join a truncated last line to the next file's first line and lose both.
    """
    session = _session_jsonl(logs_dir)
    session.write_text(session.read_text() + TRUNCATED)

    # Sorts after the truncated file, so it is the one at risk of being glued
    # onto its tail.
    _ndjson(
        logs_dir / "-home-runner-work-repo-repo2" / "session.jsonl",
        [_assistant("msg_second", FINAL_USAGE[1], final=True), {"type": "user"}],
    )

    usage = token_usage.claude_usage(
        stream=_cancelled_stream(tmp_path), logs_dir=logs_dir, model="opus"
    )

    assert usage["output_tokens"] == 6000, "lost the second session's first message"
    # The second project contributes its opening prompt and no turn of its own.
    # The subtraction is per session, so pooling the files must not count that
    # prompt as a turn.
    assert usage["turns"] == 3, "counted the second session's prompt as a turn"
    assert usage["partial"] is True


def test_survives_a_truncated_stream_json_line(tmp_path: Path, logs_dir: Path) -> None:
    """The same truncation on the stream-json must not lose a `result` event.

    Falling through to the session JSONL would still report the tokens, but as
    `partial` with an unknown cost — a needless downgrade when the result event
    itself parsed fine.
    """
    _session_jsonl(logs_dir)
    stream = _ndjson(
        tmp_path / "stream.json",
        [
            {
                "type": "result",
                "num_turns": 14,
                "total_cost_usd": 1.25,
                "usage": {"input_tokens": 23, "output_tokens": 9406},
            },
        ],
    )
    stream.write_text(stream.read_text() + '{"type":"resu')

    usage = token_usage.claude_usage(stream=stream, logs_dir=logs_dir, model="opus")

    assert usage["output_tokens"] == 9406
    assert usage["cost_usd"] == 1.25
    assert usage["partial"] is False


def test_reports_zero_when_the_agent_never_ran(logs_dir: Path) -> None:
    """No stream and no session JSONL is a genuine zero, not a partial total.

    A run that dies in preflight really did cost nothing; flagging it partial
    would push a fabricated unknown into the reports.
    """
    usage = token_usage.claude_usage(stream=None, logs_dir=logs_dir, model="opus")

    assert usage["cost_usd"] == 0
    assert usage["partial"] is False


def test_codex_sums_token_counts_across_rollouts(tmp_path: Path) -> None:
    """Codex accounting is the sum over every rollout, turns being its messages."""
    sessions = tmp_path / "sessions"
    _ndjson(
        sessions / "2026" / "08" / "25" / "rollout-a.jsonl",
        [
            {"type": "agent_message", "token_count": {"input_tokens": 100}},
            {
                "type": "token_count",
                "token_count": {
                    "input_tokens": 20,
                    "output_tokens": 30,
                    "cached_input_tokens": 90,
                },
            },
        ],
    )
    _ndjson(
        sessions / "2026" / "08" / "26" / "rollout-b.jsonl",
        [{"type": "agent_message", "token_count": {"output_tokens": 7}}],
    )

    usage = token_usage.codex_usage(
        token_usage.read_all(sorted(sessions.rglob("rollout-*.jsonl"))), "gpt-5"
    )

    assert usage == {
        "input_tokens": 120,
        "output_tokens": 37,
        "cached_input_tokens": 90,
        "turns": 2,
        "model": "gpt-5",
        "cost_usd": 0,
    }


def test_codex_counts_an_absent_token_count_as_zero() -> None:
    """Codex's rollout schema isn't versioned, so a missing field costs nothing.

    Every event carries a type; only some carry usage. An event without
    `token_count` — or with a null field inside it — must contribute 0 rather
    than failing the step and zeroing the run.
    """
    usage = token_usage.codex_usage(
        [
            {"type": "agent_message"},
            {"type": "event_msg", "token_count": None},
            {"type": "turn_context", "token_count": {"input_tokens": None}},
            {"type": "token_count", "token_count": {"input_tokens": 42}},
        ],
        "gpt-5",
    )

    assert usage["input_tokens"] == 42
    assert usage["output_tokens"] == 0
    assert usage["cached_input_tokens"] == 0
    assert usage["turns"] == 1


def test_claude_main_publishes_the_record_three_ways(
    tmp_path: Path,
    logs_dir: Path,
    github_files: GithubFiles,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: consolidate the sandbox's logs, then report from them.

    The session JSONL only reaches the log dir through the consolidating copy,
    so a broken copy shows up here as an all-zero record rather than a partial
    one.
    """
    agent_home = tmp_path / "agent-home"
    _session_jsonl(agent_home / ".claude" / "projects")
    for name in CONTEXT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_HOME", str(agent_home))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner-temp"))
    monkeypatch.setenv("MODEL", "opus")
    monkeypatch.setenv("STREAM_JSON", str(_cancelled_stream(tmp_path)))
    monkeypatch.setattr(sys, "argv", ["token_usage.py", "--harness", "claude"])

    run_command = token_usage.best_effort

    def run_without_sudo(*argv: str) -> None:
        run_command(*(argv[1:] if argv[:1] == ("sudo",) else argv))

    monkeypatch.setattr(token_usage, "best_effort", run_without_sudo)

    assert token_usage.main() == 0

    record = json.loads(github_files.outputs()["usage"])
    assert record["output_tokens"] == 4500
    assert record["partial"] is True
    # Nothing here runs under Actions, so every context key reads as absent.
    # An `if: always()` step publishes the record it has rather than failing
    # on a variable it cannot read.
    assert record["repo"] is None and record["number"] is None
    written = tmp_path / "runner-temp" / "tend-logs" / "token-usage.json"
    assert json.loads(written.read_text()) == record
    assert github_files.summary.read_text() == (
        "## Token Usage\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        "| Input | 15 |\n"
        "| Output | 4500 |\n"
        "| Cache creation | 1500 |\n"
        "| Cache read | 60000 |\n"
        "| Cost | unknown |\n"
        "| Turns | 3 |\n"
        "\n"
        f"{token_usage.PARTIAL_NOTE}\n"
        f"{token_usage.LIST_PRICE_NOTE}\n"
    )


def test_codex_main_publishes_the_record_three_ways(
    tmp_path: Path,
    github_files: GithubFiles,
    actions_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every published copy carries what the run was about, not just its spend.

    The context keys ride on the record itself so a later question — cost per
    PR, repeat runs on one commit — reads them off the artifact rather than
    joining every run back to `gh run list`. `head_sha` is the PR's head, not
    the `GITHUB_SHA` the run reports, which for a pull-request event is the
    base branch.
    """
    actions_env.write_text(
        json.dumps({"pull_request": {"number": 851, "head": {"sha": "head0"}}})
    )
    monkeypatch.setenv("GITHUB_WORKFLOW", "tend-review")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_SHA", "base0")
    home = tmp_path / "home"
    _ndjson(
        home / ".codex" / "sessions" / "2026" / "08" / "25" / "rollout-a.jsonl",
        [
            {"type": "agent_message", "token_count": {"input_tokens": 100}},
            {"type": "token_count", "token_count": {"output_tokens": 30}},
        ],
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MODEL", "gpt-5-codex")
    monkeypatch.setattr(sys, "argv", ["token_usage.py", "--harness", "codex"])

    assert token_usage.main() == 0

    record = json.loads(github_files.outputs()["usage"])
    assert record == {
        "repo": "owner/repo",
        "workflow": "tend-review",
        "run_id": 12345,
        "run_attempt": 2,
        "event": "pull_request_target",
        "number": 851,
        "head_sha": "head0",
        "input_tokens": 100,
        "output_tokens": 30,
        "cached_input_tokens": 0,
        "turns": 1,
        "model": "gpt-5-codex",
        "cost_usd": 0,
    }
    written = home / ".codex" / "projects" / "token-usage.json"
    assert json.loads(written.read_text()) == record
    assert github_files.summary.read_text() == (
        "## Token Usage (Codex)\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        "| Input | 100 |\n"
        "| Output | 30 |\n"
        "| Cached input | 0 |\n"
        "| Turns | 1 |\n"
        "| Model | gpt-5-codex |\n"
        "\n"
        f"{token_usage.CODEX_COST_NOTE}\n"
    )


def test_cost_renders_to_the_cent_and_says_so_when_unknown() -> None:
    """The summary's cost cell, across the three shapes cost_usd takes."""
    assert token_usage.cell(None, "cost_usd") == "unknown"
    assert token_usage.cell(0, "cost_usd") == "$0.00"
    assert token_usage.cell(1.2, "cost_usd") == "$1.20"
