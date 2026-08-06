"""Tests for the composite actions' shared step bodies (shared/steps/*.sh).

These scripts run as `bash <script>` inside both harness actions, so a
non-zero exit fails the step and turns an otherwise-successful agent run red.
They aren't part of the generator package; the tests live here because this is
the repo's only Python suite, and shellcheck (pre-commit) can't catch runtime
behaviour.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MARK_NOTIFICATION_READ = REPO_ROOT / "shared" / "steps" / "mark-notification-read.sh"
COMPUTE_TOKEN_USAGE = REPO_ROOT / "shared" / "steps" / "compute-token-usage.sh"

# `gh api` stand-in. Records every invocation so a test can assert which calls
# the script made, and fails the run-metadata fetch when FAIL_RUN_META is set.
FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$2" in
  repos/*/actions/runs/*)
    [ -n "${FAIL_RUN_META:-}" ] && exit 1
    echo "$FAKE_RUN_STARTED_AT"
    ;;
  notifications)
    cat "$NOTIFICATIONS_JSON"
    ;;
  notifications/threads/*)
    ;;
  *)
    exit 1
    ;;
esac
"""


@pytest.fixture
def gh_env(tmp_path: Path) -> dict[str, str]:
    """A fake `gh` on PATH plus the Actions env the script reads."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"issue": {"number": 7}}))

    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "12345",
        # Deliberately not `RUN_STARTED_AT`: the script assigns that name, and
        # an inherited value would let the happy-path tests pass even if the
        # fetched timestamp were never used.
        "FAKE_RUN_STARTED_AT": "2026-01-02T00:00:00Z",
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(MARK_NOTIFICATION_READ)],
        env=env,
        capture_output=True,
        text=True,
    )


def _calls(env: dict[str, str]) -> list[str]:
    return Path(env["GH_CALLS"]).read_text().splitlines()


def _notifications(tmp_path: Path, updated_at: str) -> str:
    """One unread notification for issue 7 of owner/repo."""
    path = tmp_path / "notifications.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "999",
                    "updated_at": updated_at,
                    "subject": {
                        "url": "https://api.github.com/repos/owner/repo/issues/7"
                    },
                }
            ]
        )
    )
    return str(path)


def test_mark_notification_read_tolerates_run_metadata_failure(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """A transient failure fetching `run_started_at` must not fail the step.

    The script runs under `set -e`, so an unguarded `gh api` there aborts it
    non-zero. The step is gated on `if: success()` in both harness actions, so
    that exit turns a fully-successful agent run into a red job. The correct
    disposition is to skip this cycle and leave the thread unread — the
    scheduled tend-notifications poll picks it up.
    """
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-01-01T00:00:00Z")
    gh_env["FAIL_RUN_META"] = "1"

    result = _run(gh_env)

    assert result.returncode == 0, (
        f"script aborted on a transient run-metadata error (exit "
        f"{result.returncode}); stderr:\n{result.stderr}"
    )
    # Without the timestamp the `updated_at <= started` guard can't be
    # evaluated, so nothing may be marked read.
    assert not any("-X PATCH" in c for c in _calls(gh_env)), (
        "marked a thread read without knowing when the run started"
    )


def test_mark_notification_read_marks_thread_predating_the_run(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """The happy path still marks a thread whose activity predates the run."""
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-01-01T00:00:00Z")

    result = _run(gh_env)

    assert result.returncode == 0, result.stderr
    assert "api notifications/threads/999 -X PATCH" in _calls(gh_env)


def test_mark_notification_read_leaves_activity_newer_than_the_run(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """Activity that arrived after the run started stays unread."""
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-03-01T00:00:00Z")

    result = _run(gh_env)

    assert result.returncode == 0, result.stderr
    assert not any("-X PATCH" in c for c in _calls(gh_env))


# --- compute-token-usage.sh -------------------------------------------------
#
# Fixtures below mirror the shapes observed in real uploaded artifacts. Three
# properties drive the tests:
#
# 1. Both files record each assistant message roughly twice, so any sum has to
#    deduplicate by `.message.id` or it lands ~2x high.
# 2. The stream-json's assistant events are non-final (`stop_reason: null`):
#    their `usage.output_tokens` is the message-start placeholder (single
#    digits), not the finished count. Only the session JSONL carries final
#    per-message usage. Reconstructing from the stream-json therefore
#    under-counts output by orders of magnitude, while input and cache fields
#    — known at message start — happen to match.
# 3. A session that ran a `Task` has a second transcript under
#    `<session-id>/subagents/`, whose usage the `result` event does not count.


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
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


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
STREAM_USAGE = [dict(u, output_tokens=6) for u in FINAL_USAGE]


# A `Task` subagent's own transcript, which real artifacts carry alongside the
# session it belongs to. Its usage is not in the `result` event, so nothing
# here may reach the totals.
SUBAGENT_USAGE = {
    "input_tokens": 300,
    "output_tokens": 7000,
    "cache_creation_input_tokens": 40000,
    "cache_read_input_tokens": 900000,
}


def _session_jsonl(logs_dir: Path) -> Path:
    """A cancelled session's JSONL: real usage, each message duplicated.

    Writes the subagent transcript beside it too — `<session>/subagents/` is
    how Claude Code lays a `Task` out on disk, and `cp -a .../projects/.`
    copies the subtree into LOGS_DIR.
    """
    project = logs_dir / "-home-runner-work-repo-repo"
    project.mkdir(parents=True, exist_ok=True)
    lines: list[dict[str, object]] = [{"type": "user"}]
    for i, usage in enumerate(FINAL_USAGE):
        entry = _assistant(f"msg_{i}", usage, final=True)
        lines += [entry, dict(entry), {"type": "user"}]
    lines.append({"type": "user"})

    subagents = project / "session" / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    _ndjson(
        subagents / "agent-a1b2c3.jsonl",
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


def _usage(tmp_path: Path, *, stream: Path | None, logs_dir: Path) -> dict[str, object]:
    result = subprocess.run(
        ["bash", str(COMPUTE_TOKEN_USAGE)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "MODEL": "opus",
            "LOGS_DIR": str(logs_dir),
            "STREAM_JSON": str(stream) if stream else "",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_token_usage_reconstructs_a_cancelled_session(tmp_path: Path) -> None:
    """A cancelled session must be accounted from its session JSONL.

    `tend-review` runs with `cancel-in-progress: true`, so cancellation is
    routine — and a cancelled session never emits a `type: "result"` event.
    The step is `if: always()`, so it still writes token-usage.json and still
    uploads the artifact; only the accounting is lost. Reporting zeros for a
    run that did real work (and may already have posted a review) biases every
    downstream total by the cancellation rate.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    assert usage["output_tokens"] == 4500, (
        f"cancelled session reported output_tokens={usage['output_tokens']}; "
        "the session JSONL records 4500 across two messages"
    )
    assert usage["input_tokens"] == 15
    assert usage["cache_creation_input_tokens"] == 1500
    assert usage["cache_read_input_tokens"] == 60000
    # Three `user` lines bracket the two assistant turns; num_turns counts the
    # turns between them.
    assert usage["turns"] == 3
    assert usage["partial"] is True, (
        "a reconstructed total must be distinguishable from a run that "
        "genuinely cost nothing"
    )


def test_token_usage_ignores_stream_json_placeholder_output(tmp_path: Path) -> None:
    """The fallback must not sum the stream-json's non-final assistant events.

    They carry `stop_reason: null` and a message-start `output_tokens`, so
    summing them under-counts output by orders of magnitude while input and
    cache fields still match — a wrong number that looks plausible.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    stream_sum = sum(u["output_tokens"] for u in STREAM_USAGE)
    assert usage["output_tokens"] != stream_sum, (
        "summed the stream-json's placeholder output_tokens"
    )


def test_token_usage_ignores_subagent_transcripts(tmp_path: Path) -> None:
    """Subagent transcripts must not be slurped into the reconstruction.

    Every `Task` writes its own `<session>/subagents/agent-*.jsonl`, but the
    `result` event this fallback stands in for counts only the main loop.
    Summing both inflates each field — turns roughly doubles — so a partial
    run would no longer be comparable with a complete one.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    assert usage["output_tokens"] == 4500, "summed the subagent's output_tokens"
    assert usage["cache_read_input_tokens"] == 60000, "summed the subagent's cache"
    assert usage["turns"] == 3, "counted the subagent's `user` lines as turns"


def test_token_usage_prefers_result_events_when_present(tmp_path: Path) -> None:
    """A completed session still reports straight from its `result` events."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)
    stream = _ndjson(
        tmp_path / "stream.json",
        [
            _assistant("msg_0", STREAM_USAGE[0], final=False),
            {
                "type": "result",
                "num_turns": 14,
                "total_cost_usd": 1.2563179999999998,
                "usage": {
                    "input_tokens": 23,
                    "output_tokens": 9406,
                    "cache_creation_input_tokens": 62655,
                    "cache_read_input_tokens": 789006,
                },
            },
        ],
    )

    usage = _usage(tmp_path, stream=stream, logs_dir=logs_dir)

    assert usage["output_tokens"] == 9406
    assert usage["turns"] == 14
    assert usage["cost_usd"] == 1.26
    assert usage["partial"] is False


def test_token_usage_survives_a_truncated_final_line(tmp_path: Path) -> None:
    """A half-written line costs that line, not the run's whole accounting.

    A cancelled process can be killed mid-append, leaving its session JSONL
    ending in a partial entry. `jq -s` aborts the file on the first parse
    error and the `|| echo ''` swallows it, which would drop the run into the
    "agent never ran" branch — republishing the all-zero `partial: false`
    payload this fallback exists to replace, now indistinguishable from a
    genuine preflight no-op.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    session = _session_jsonl(logs_dir)
    session.write_text(session.read_text() + '{"type":"assistant","mess')

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    assert usage["output_tokens"] == 4500, "a truncated tail zeroed the totals"
    assert usage["turns"] == 3
    assert usage["partial"] is True


def test_token_usage_survives_a_truncated_stream_json_line(tmp_path: Path) -> None:
    """The same truncation on the stream-json must not lose a `result` event.

    Falling through to the session JSONL would still report the tokens, but as
    `partial` with an unknown cost — a needless downgrade when the result event
    itself parsed fine.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
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

    usage = _usage(tmp_path, stream=stream, logs_dir=logs_dir)

    assert usage["output_tokens"] == 9406
    assert usage["cost_usd"] == 1.25
    assert usage["partial"] is False


def test_token_usage_reports_zero_when_the_agent_never_ran(tmp_path: Path) -> None:
    """No stream and no session JSONL is a genuine zero, not a partial total.

    A run that dies in preflight really did cost nothing; flagging it partial
    would push a fabricated unknown into the reports.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    usage = _usage(tmp_path, stream=None, logs_dir=logs_dir)

    assert usage["output_tokens"] == 0
    assert usage["cost_usd"] == 0
    assert usage["partial"] is False
