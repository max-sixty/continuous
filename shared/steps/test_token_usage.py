"""Tests for the token-usage step body.

The Claude fixtures mirror the shapes observed in real uploaded artifacts, and
``output_tokens == 4500`` is what discriminates between the three accounting
paths: the stream-json's message-start placeholders sum to 12, and the `Task`
subagent's transcript beside the session would add 7000.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
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

# The keys `run_context` contributes to the record.
CONTEXT_KEYS = (
    "repo",
    "workflow",
    "run_id",
    "run_attempt",
    "event",
    "number",
    "head_sha",
)
# The runner's own channels, which a test needs even with the rest of the
# Actions environment stripped.
GITHUB_FILE_VARS = ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_STEP_SUMMARY")


def _assistant(msg_id: str, usage: dict[str, int], *, final: bool) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "stop_reason": "end_turn" if final else None,
            "usage": usage,
        },
    }


def _codex_token_count(**usage: int) -> dict[str, object]:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": usage},
        },
    }


def _codex_message() -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant"},
    }


def _ndjson(path: Path, lines: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _session_jsonl(logs_dir: Path) -> Path:
    """A cancelled session's JSONL: real usage, each message duplicated.

    Writes the subagent transcript beside it too — ``<session>/subagents/`` is
    how Claude Code lays a ``Task`` out on disk, and the bounded tree exporter
    preserves the subtree in the log dir.
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


@pytest.fixture
def reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness's record that every sandbox process is dead.

    `consolidate_logs` copies nothing without it, so a test that exercises the
    copy has to say the reap happened.
    """
    monkeypatch.setenv("SANDBOX_REAPED", "true")


def _run_without_sudo(run_command: Callable[..., None], *argv: str) -> None:
    if argv[:2] == ("/usr/bin/sudo", "-n") and "--copy-tree" in argv:
        start = argv.index("--copy-tree") + 1
        source, destination, uid, gid = argv[start:]
        try:
            token_usage.privileged_copy(
                Path(source), Path(destination), uid=int(uid), gid=int(gid)
            )
        except (OSError, ValueError):
            pass
        return
    run_command(*argv)


@pytest.fixture
def sudoless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the consolidating copy's commands unprivileged, as themselves.

    The copy runs as root on a runner because the source belongs to another
    UID; under a test the source is the test's own, so dropping the `sudo`
    prefix leaves each command doing exactly what it does in CI.
    """
    run_command = token_usage.best_effort

    def run_without_sudo(*argv: str) -> None:
        _run_without_sudo(run_command, *argv)

    monkeypatch.setattr(token_usage, "best_effort", run_without_sudo)


def _record_commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Capture what the consolidating copy would run instead of running it."""
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(token_usage, "best_effort", lambda *argv: commands.append(argv))
    return commands


def _aimed_inside(
    commands: list[tuple[str, ...]], *paths: Path
) -> list[tuple[str, ...]]:
    """The captured commands naming any of *paths* — none, when a guard held."""
    return [
        argv
        for argv in commands
        if any(str(path) in arg for arg in argv for path in paths)
    ]


def test_reconstructs_a_cancelled_session(tmp_path: Path, logs_dir: Path) -> None:
    """A cancelled session must be accounted from its session JSONL.

    `tend-triage` runs with `cancel-in-progress: true`, so cancellation is
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


def test_codex_sums_final_cumulative_counts_across_rollouts() -> None:
    """Each rollout's last count is cumulative; independent rollouts are additive."""
    usage = token_usage.codex_usage(
        [
            [
                {"type": "turn_context", "payload": {"model": "gpt-recommended"}},
                _codex_token_count(
                    input_tokens=20, output_tokens=5, cached_input_tokens=10
                ),
                _codex_message(),
                _codex_token_count(
                    input_tokens=100, output_tokens=30, cached_input_tokens=90
                ),
            ],
            [
                _codex_message(),
                _codex_token_count(input_tokens=20, output_tokens=7),
            ],
        ],
        "",
    )

    assert usage == {
        "input_tokens": 120,
        "output_tokens": 37,
        "cached_input_tokens": 90,
        "turns": 2,
        "model": "gpt-recommended",
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
            [
                {"type": "response_item", "payload": {"type": "reasoning"}},
                {"type": "event_msg", "payload": {"type": "token_count"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": None}},
                    },
                },
                {"type": "turn_context", "payload": {"model": "gpt-ignored"}},
            ]
        ],
        "gpt-5",
    )

    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["cached_input_tokens"] == 0
    assert usage["turns"] == 0


def test_claude_main_publishes_the_record_three_ways(
    tmp_path: Path,
    logs_dir: Path,
    github_files: GithubFiles,
    monkeypatch: pytest.MonkeyPatch,
    sudoless: None,
    reaped: None,
) -> None:
    """End to end: consolidate the sandbox's logs, then report from them.

    The session JSONL only reaches the log dir through the consolidating copy,
    so a broken copy shows up here as an all-zero record rather than a partial
    one.
    """
    agent_home = tmp_path / "agent-home"
    _session_jsonl(agent_home / ".claude" / "projects")
    # Every `GITHUB_*` rather than a list of the ones `run_context` reads:
    # pytest itself runs under Actions in CI, and a hand-kept list goes stale
    # the moment a key is added, silently reading the ambient value instead.
    for name in [name for name in os.environ if name.startswith("GITHUB_")]:
        if name not in GITHUB_FILE_VARS:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_HOME", str(agent_home))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner-temp"))
    monkeypatch.setenv("MODEL", "opus")
    monkeypatch.setenv("STREAM_JSON", str(_cancelled_stream(tmp_path)))
    monkeypatch.setattr(sys, "argv", ["token_usage.py", "--harness", "claude"])

    assert token_usage.main() == 0

    record = json.loads(github_files.outputs()["usage"])
    assert record["output_tokens"] == 4500
    assert record["partial"] is True
    # Nothing here runs under Actions, so every context key reads as absent.
    # An `if: always()` step publishes the record it has rather than failing
    # on a variable it cannot read.
    context = {key: record[key] for key in CONTEXT_KEYS}
    assert context == dict.fromkeys(CONTEXT_KEYS)
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
    home = tmp_path / "agent-home"
    _ndjson(
        home / ".codex" / "sessions" / "2026" / "08" / "25" / "rollout-a.jsonl",
        [
            _codex_message(),
            _codex_token_count(input_tokens=100, output_tokens=30),
        ],
    )
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setenv("SANDBOX_REAPED", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner-temp"))
    monkeypatch.setenv("MODEL", "gpt-5-codex")
    monkeypatch.setattr(sys, "argv", ["token_usage.py", "--harness", "codex"])

    run_command = token_usage.best_effort

    def run_without_sudo(*argv: str) -> None:
        _run_without_sudo(run_command, *argv)

    monkeypatch.setattr(token_usage, "best_effort", run_without_sudo)

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
    written = tmp_path / "runner-temp" / "tend-codex-logs" / "token-usage.json"
    assert json.loads(written.read_text()) == record
    assert github_files.summary.read_text() == (
        "## Token Usage (Codex)\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        "| Total input | 100 |\n"
        "| Output | 30 |\n"
        "| Cached input (included in total) | 0 |\n"
        "| Turns | 1 |\n"
        "| Model | gpt-5-codex |\n"
        "\n"
        f"{token_usage.CODEX_COST_NOTE}\n"
    )


def test_codex_accounting_waits_for_the_sandbox_to_be_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "agent-home" / ".codex" / "sessions"
    _ndjson(sessions / "rollout.jsonl", [{"token_count": {"input_tokens": 100}}])
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "agent-home"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner-temp"))
    monkeypatch.delenv("SANDBOX_REAPED", raising=False)
    commands = _record_commands(monkeypatch)

    usage, logs_dir = token_usage.codex_step("gpt-5-codex")

    assert _aimed_inside(commands, sessions) == []
    assert not (logs_dir / "sessions").exists()
    assert usage["input_tokens"] == 0


def test_agent_log_copy_drops_nested_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "agent" / ".codex" / "sessions"
    source.mkdir(parents=True)
    (source / "rollout.jsonl").write_text("{}\n")
    (source / "escape").symlink_to("/etc/passwd")
    destination = tmp_path / "logs"

    run_command = token_usage.best_effort

    def run_without_sudo(*argv: str) -> None:
        _run_without_sudo(run_command, *argv)

    monkeypatch.setattr(token_usage, "best_effort", run_without_sudo)
    token_usage.copy_agent_tree(source, destination)

    assert (destination / "rollout.jsonl").is_file()
    assert not (destination / "escape").exists()


def test_agent_log_copy_refuses_symlink_source(tmp_path: Path, sudoless: None) -> None:
    real = tmp_path / "real"
    real.mkdir()
    source = tmp_path / "agent" / ".codex" / "sessions"
    source.parent.mkdir(parents=True)
    source.symlink_to(real, target_is_directory=True)
    destination = tmp_path / "logs"

    token_usage.copy_agent_tree(source, destination)

    assert list(destination.iterdir()) == []


def test_cost_renders_to_the_cent_and_says_so_when_unknown() -> None:
    """The summary's cost cell, across the three shapes cost_usd takes."""
    assert token_usage.cell(None, "cost_usd") == "unknown"
    assert token_usage.cell(0, "cost_usd") == "$0.00"
    assert token_usage.cell(1.2, "cost_usd") == "$1.20"


def test_consolidation_refuses_a_symlinked_session_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sudoless: None,
    reaped: None,
) -> None:
    """The root helper refuses a symlink at the session-tree boundary.

    The agent owns the parent of `.claude/projects`, so it chooses what the
    runner's root-privileged copy reads. Pointing it at a tree of the agent's
    choosing publishes that tree; refusing the link is the fix.
    """
    agent_home = tmp_path / "agent-home"
    (agent_home / ".claude").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ca-key.pem").write_text("PRIVATE KEY\n")
    (agent_home / ".claude" / "projects").symlink_to(elsewhere)
    monkeypatch.setenv("AGENT_HOME", str(agent_home))
    token_usage.consolidate_logs(tmp_path / "logs", tmp_path / "runner-temp")

    assert list((tmp_path / "logs").iterdir()) == []


def test_consolidation_refuses_a_symlinked_dot_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sudoless: None,
    reaped: None,
) -> None:
    """The dot-dir the session dir sits in is a boundary too.

    Checking only `.claude/projects` leaves `.claude` itself free to be a link:
    the session dir under it is then a real directory and passes, while the
    privileged copy reads out of whatever tree the agent aimed the dot-dir at.
    """
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "projects").mkdir(parents=True)
    (elsewhere / "projects" / "session.jsonl").write_text("{}\n")
    (agent_home / ".claude").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setenv("AGENT_HOME", str(agent_home))
    token_usage.consolidate_logs(tmp_path / "logs", tmp_path / "runner-temp")

    assert list((tmp_path / "logs").iterdir()) == []


def test_consolidation_copies_nothing_when_the_agent_never_wrote_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sudoless: None,
    reaped: None,
) -> None:
    """`AGENT_HOME` is unset when sandbox setup died before exporting it.

    The placeholder home names nothing, and neither does a home whose agent
    never opened a session — both read as nothing to copy, without a privileged
    command run against a path that isn't there.
    """
    monkeypatch.delenv("AGENT_HOME", raising=False)
    token_usage.consolidate_logs(tmp_path / "logs", tmp_path / "runner-temp")

    assert list((tmp_path / "logs").iterdir()) == []


def test_consolidation_drops_symlinks_from_the_uploaded_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sudoless: None, reaped: None
) -> None:
    """The bounded exporter omits links before upload-artifact sees the tree.

    A link the agent plants in its session dir would otherwise publish whatever
    the runner can read — the proxy's CA private key, its own
    `/proc/<pid>/environ` — into a downloadable artifact.
    """
    agent_home = tmp_path / "agent-home"
    projects = agent_home / ".claude" / "projects" / "project"
    projects.mkdir(parents=True)
    (projects / "session.jsonl").write_text("{}\n")
    secret = tmp_path / "ca-key.pem"
    secret.write_text("PRIVATE KEY\n")
    (projects / "leak.pem").symlink_to(secret)
    monkeypatch.setenv("AGENT_HOME", str(agent_home))

    logs_dir = tmp_path / "logs"
    token_usage.consolidate_logs(logs_dir, tmp_path / "runner-temp")

    assert (logs_dir / "project" / "session.jsonl").is_file()
    assert not (logs_dir / "project" / "leak.pem").exists()
    assert secret.read_text() == "PRIVATE KEY\n"


def test_consolidation_waits_for_the_sandbox_to_be_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A surviving agent can swap the session dir between the check and the copy.

    `copy_agent_tree` checks the tree it is about to read as root, so the check
    holds only once nothing is left running to change it. The supervisor
    publishes the reap, and without it the run reports its usage from the
    stream-json and uploads no session logs.
    """
    agent_home = tmp_path / "agent-home"
    projects = agent_home / ".claude" / "projects" / "project"
    projects.mkdir(parents=True)
    (projects / "session.jsonl").write_text("{}\n")
    monkeypatch.setenv("AGENT_HOME", str(agent_home))
    monkeypatch.delenv("SANDBOX_REAPED", raising=False)
    commands = _record_commands(monkeypatch)

    token_usage.consolidate_logs(tmp_path / "logs", tmp_path / "runner-temp")

    assert _aimed_inside(commands, agent_home) == []
    assert list((tmp_path / "logs").iterdir()) == []


def test_consolidation_drops_a_symlink_a_directory_mode_would_shield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sudoless: None, reaped: None
) -> None:
    """The bounded exporter creates normal runner-owned directory modes.

    Unlinking needs write on the parent, so a link the agent leaves in a `0555`
    directory outlives the sweep and takes the `if: always()` step down
    with a `PermissionError` on the way. A `0000` directory is the same cause
    with a quieter outcome: `rglob` yields nothing and the link just stays. The
    copy normalises the modes it lands, which covers both.
    """
    agent_home = tmp_path / "agent-home"
    project = agent_home / ".claude" / "projects" / "project"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}\n")
    secret = tmp_path / "ca-key.pem"
    secret.write_text("PRIVATE KEY\n")
    (project / "leak.pem").symlink_to(secret)
    project.chmod(0o555)
    monkeypatch.setenv("AGENT_HOME", str(agent_home))

    logs_dir = tmp_path / "logs"
    try:
        token_usage.consolidate_logs(logs_dir, tmp_path / "runner-temp")
    finally:
        project.chmod(0o700)

    assert (logs_dir / "project" / "session.jsonl").is_file()
    assert not (logs_dir / "project" / "leak.pem").is_symlink()
    assert secret.read_text() == "PRIVATE KEY\n"


def test_consolidation_drops_a_fifo_the_agent_planted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sudoless: None, reaped: None
) -> None:
    """The bounded exporter omits a FIFO before the upload walks the tree.

    `upload-artifact` reads every entry that isn't a directory, and opening a
    FIFO with no writer blocks — so one left in the session dir hangs an
    `if: always()` upload until the job times out. `mkfifo` needs no privilege,
    so the agent chooses whether that happens.
    """
    agent_home = tmp_path / "agent-home"
    project = agent_home / ".claude" / "projects" / "project"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("{}\n")
    os.mkfifo(project / "pipe")
    monkeypatch.setenv("AGENT_HOME", str(agent_home))

    logs_dir = tmp_path / "logs"
    token_usage.consolidate_logs(logs_dir, tmp_path / "runner-temp")

    assert (logs_dir / "project" / "session.jsonl").is_file()
    assert not (logs_dir / "project" / "pipe").exists()
