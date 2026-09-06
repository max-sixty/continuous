"""Tests for the Codex harness's cohesive runner commands."""

from __future__ import annotations

import base64
import importlib.util
import subprocess
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[2] / "codex" / "runner.py"
SPEC = importlib.util.spec_from_file_location("tend_codex_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
codex_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_runner)


def _result(args: list[str], *, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args, returncode, stdout, "")


def test_install_plugin_exports_the_single_reported_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    action = tmp_path / "repo/codex"
    action.mkdir(parents=True)
    plugin = tmp_path / "installed/tend-ci-runner"
    plugin.mkdir(parents=True)
    github_env = tmp_path / "github-env"
    monkeypatch.setenv("ACTION_PATH", str(action))
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    calls: list[list[str]] = []

    def run(args: list[str], **_: object):
        calls.append(args)
        stdout = f"Installed plugin root: {plugin}\n" if args[2:3] == ["add"] else ""
        return _result(args, stdout=stdout)

    monkeypatch.setattr(codex_runner, "_run", run)

    assert codex_runner.main(["install-plugin"]) == 0
    assert github_env.read_text() == f"CLAUDE_PLUGIN_ROOT={plugin}\n"
    assert capsys.readouterr().out == f"Installed plugin root: {plugin}\n"
    assert calls == [
        ["codex", "plugin", "marketplace", "add", str(action.parent)],
        ["codex", "plugin", "add", "tend-ci-runner@tend"],
        ["codex", "plugin", "list"],
    ]


def test_install_plugin_rejects_ambiguous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = tmp_path / "repo/codex"
    action.mkdir(parents=True)
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    monkeypatch.setenv("ACTION_PATH", str(action))
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github-env"))
    monkeypatch.setattr(
        codex_runner,
        "_run",
        lambda args, **kwargs: _result(
            args,
            stdout=(
                f"Installed plugin root: {plugin}\nInstalled plugin root: {plugin}\n"
                if kwargs.get("capture")
                else ""
            ),
        ),
    )

    assert codex_runner.main(["install-plugin"]) == 1


def test_stage_agents_substitutes_only_the_bot_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = tmp_path / "repo/codex"
    shared = tmp_path / "repo/shared"
    home = tmp_path / "home"
    action.mkdir(parents=True)
    shared.mkdir()
    (shared / "system-prompt.md").write_text("Act as ${BOT_NAME}; keep $GH_TOKEN.\n")
    (action / "agents-tail.md").write_text("Look up $BOT_NAME.\n")
    monkeypatch.setenv("ACTION_PATH", str(action))
    monkeypatch.setenv("BOT_NAME", "tend-bot")
    monkeypatch.setenv("HOME", str(home))

    assert codex_runner.main(["stage-agents"]) == 0

    assert (home / ".codex/AGENTS.md").read_text() == (
        "# Tend CI guidance (Codex harness)\n\n"
        "Act as tend-bot; keep $GH_TOKEN.\n\n"
        "Look up tend-bot.\n"
    )


def test_run_exports_the_final_message_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MODEL", "gpt-test")
    monkeypatch.setenv("SANDBOX", "workspace-write")
    monkeypatch.setenv("EFFORT", "high")
    monkeypatch.setenv("PROMPT", "Review this")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object):
        calls.append((args, kwargs))
        Path(args[args.index("--output-last-message") + 1]).write_bytes(b"final\n")
        return _result(args, returncode=7)

    monkeypatch.setattr(codex_runner, "_run", run)

    assert codex_runner.main(["run"]) == 7
    assert github_output.read_text() == (
        "final_message=" + base64.b64encode(b"final\n").decode() + "\n"
    )
    args, kwargs = calls[0]
    assert args == [
        "codex",
        "exec",
        "--model",
        "gpt-test",
        "--sandbox",
        "workspace-write",
        "--output-last-message",
        str(tmp_path / "codex-final-message.md"),
        "--config",
        'model_reasoning_effort="high"',
        "Review this",
    ]
    assert kwargs["check"] is False
    assert kwargs["env"]["PATH"] == f"/usr/bin:{tmp_path}/tend-agent-uv"
