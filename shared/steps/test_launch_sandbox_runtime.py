"""Contracts for the single trusted post-sandbox result bottleneck."""

from __future__ import annotations

import base64
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import launch_sandbox_runtime as launch
import pytest


def configure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, harness: str
) -> tuple[Path, Path, Path]:
    runner_temp = tmp_path / "runner-temp"
    run_dir = tmp_path / "agent-home" / "run"
    runner_temp.mkdir()
    run_dir.mkdir(parents=True)
    agent_env = runner_temp / "agent-env"
    agent_env.write_text("HOME=/home/tend-sandbox\nGITHUB_TOKEN=dummy\n")
    output = runner_temp / "github-output"
    output.touch()
    summary = runner_temp / "step-summary"
    summary.touch()
    for name, value in {
        "SANDBOX": "tend-sandbox",
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(output),
        "TEND_RUN_DIR": str(run_dir),
        "AGENT_ENV_FILE": str(agent_env),
        "NODE_BIN": "/trusted/node",
        "TEND_HARNESS": harness,
        "AGENT_HOME": str(run_dir.parent),
        "TEND_STEP_SUMMARY_DIR": str(run_dir.parent),
        "GITHUB_STEP_SUMMARY": str(summary),
    }.items():
        monkeypatch.setenv(name, value)
    return run_dir, output, summary


def fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    *,
    harness: str,
    write_summary: bool = True,
    reaped: bool = True,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["/usr/bin/pgrep", "-u"]:
            return subprocess.CompletedProcess(args, 1 if reaped else 0)
        if "sandbox_runtime.mjs" in args[-1]:
            # The inner GitHub output is deliberately attacker-writable. Its
            # contents never cross; the outer supervisor exports fixed files.
            (run_dir / "tend-step-output").write_text(
                "sandbox_reaped=false\nstream_json=/etc/passwd\n"
            )
            if harness == "claude":
                (run_dir / "tend-stream.json").write_bytes(b'{"type":"result"}\n')
                (run_dir / "tend-claude-stderr.log").write_bytes(b"diagnostic\n")
            else:
                (run_dir / "codex-final-message.md").write_bytes(b"finished\n")
            if write_summary:
                (run_dir.parent / "step-summary.md").write_bytes(b"skill result\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_claude_exports_only_fixed_runner_owned_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, output, summary = configure(tmp_path, monkeypatch, harness="claude")
    monkeypatch.setenv("GITHUB_TOKEN", "runner-token-must-not-cross")
    monkeypatch.setenv("OPENAI_API_KEY", "runner-key-must-not-cross")
    monkeypatch.setenv("GITHUB_ACTOR", "octocat")
    calls = fake_runtime(monkeypatch, run_dir, harness="claude")

    assert launch.main() == 0

    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["sandbox_reaped"] == "true"
    exported = Path(values["stream_json"])
    assert exported.read_bytes() == b'{"type":"result"}\n'
    assert exported.is_relative_to(tmp_path / "runner-temp")
    assert (
        tmp_path / "runner-temp/tend-claude-stderr.log"
    ).read_bytes() == b"diagnostic\n"
    assert values.get("stream_json") != "/etc/passwd"
    assert summary.read_bytes() == b"skill result\n\n"
    runtime = next(args for args in calls if "sandbox_runtime.mjs" in args[-1])
    assert "GITHUB_TOKEN=dummy" in runtime
    assert "GITHUB_ACTOR=octocat" in runtime
    assert not any("runner-token-must-not-cross" in arg for arg in runtime)
    assert not any("runner-key-must-not-cross" in arg for arg in runtime)
    assert f"GITHUB_OUTPUT={run_dir / 'tend-step-output'}" in runtime
    assert f"GITHUB_STEP_SUMMARY={run_dir.parent / 'step-summary.md'}" in runtime


def test_codex_base64_encodes_the_fixed_final_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, output, summary = configure(tmp_path, monkeypatch, harness="codex")
    fake_runtime(monkeypatch, run_dir, harness="codex")

    assert launch.main() == 0

    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert base64.b64decode(values["final_message"]) == b"finished\n"
    assert values["sandbox_reaped"] == "true"
    assert summary.read_bytes() == b"skill result\n\n"


def test_agent_step_summary_symlink_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, _output, summary = configure(tmp_path, monkeypatch, harness="codex")
    secret = tmp_path / "runner-secret"
    secret.write_text("must not cross\n")
    (run_dir.parent / "step-summary.md").symlink_to(secret)
    fake_runtime(monkeypatch, run_dir, harness="codex", write_summary=False)

    assert launch.main() == 0
    assert summary.read_bytes() == b""


def test_no_agent_owned_result_is_read_until_the_uid_is_quiescent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, output, summary = configure(tmp_path, monkeypatch, harness="claude")
    fake_runtime(monkeypatch, run_dir, harness="claude", reaped=False)

    assert launch.main() == 1

    assert output.read_text() == "sandbox_reaped=false\n"
    assert summary.read_bytes() == b""
    assert not (tmp_path / "runner-temp/tend-agent-export/claude-stream.json").exists()


def test_runner_cancellation_is_raised_through_the_reap_path() -> None:
    previous = signal.getsignal(signal.SIGTERM)

    with pytest.raises(launch.Cancelled) as raised, launch.raise_on_cancel():
        os.kill(os.getpid(), signal.SIGTERM)

    assert raised.value.signum == signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is previous
