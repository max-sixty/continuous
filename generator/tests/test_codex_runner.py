"""Tests for the Codex harness's credential-isolated runner commands."""

from __future__ import annotations

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


def _set_sandbox_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    action = tmp_path / "repo/codex"
    action.mkdir(parents=True)
    (action.parent / ".agents").mkdir()
    (action.parent / "plugins").mkdir()
    agent_home = tmp_path / "sandbox-home"
    agent_home.mkdir()
    agent_env = tmp_path / "agent-env"
    agent_env.write_text(
        f"HOME={agent_home}\n"
        "PATH=/usr/bin\n"
        "GH_TOKEN=ghp_tendproxydummy\n"
        "GITHUB_TOKEN=ghp_tendproxydummy\n"
    )
    monkeypatch.setenv("ACTION_PATH", str(action))
    monkeypatch.setenv("AGENT_HOME", str(agent_home))
    monkeypatch.setenv("AGENT_ENV_FILE", str(agent_env))
    monkeypatch.setenv("SANDBOX", "tend-sandbox")
    monkeypatch.setenv("CODEX_BIN", "/opt/codex/bin/codex")
    return action, agent_home, agent_env


def test_install_plugin_exports_the_single_sandbox_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, agent_home, agent_env = _set_sandbox_env(tmp_path, monkeypatch)
    plugin = agent_home / ".codex/plugins/tend-ci-runner"
    plugin.mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object):
        calls.append((args, kwargs))
        stdout = (
            f"Installed plugin root: {plugin}\n"
            if args[-3:] == ["plugin", "add", "tend-ci-runner@tend"]
            else ""
        )
        return _result(args, stdout=stdout)

    monkeypatch.setattr(codex_runner, "_run", run)

    assert codex_runner.main(["install-plugin"]) == 0
    assert agent_env.read_text().endswith(f"CLAUDE_PLUGIN_ROOT={plugin}\n")
    assert capsys.readouterr().out == f"Installed plugin root: {plugin}\n"
    marketplace = agent_home / "tend-marketplace"
    assert calls[0][0] == [
        "/usr/bin/sudo",
        "/usr/bin/rm",
        "-rf",
        "--",
        str(marketplace),
    ]
    codex_calls = [args for args, _ in calls if "/opt/codex/bin/codex" in args]
    assert len(codex_calls) == 4
    assert all(
        args[:5]
        == ["/usr/bin/sudo", "-u", "tend-sandbox", "/usr/bin/env", f"HOME={agent_home}"]
        for args in codex_calls
    )
    assert codex_calls[0][-5:] == [
        "/opt/codex/bin/codex",
        "plugin",
        "marketplace",
        "add",
        str(marketplace),
    ]
    assert codex_calls[1][-3:] == ["plugin", "add", "install-tend@tend"]
    assert codex_calls[2][-3:] == ["plugin", "add", "tend-ci-runner@tend"]


def test_install_plugin_rejects_a_root_outside_the_sandbox_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_sandbox_env(tmp_path, monkeypatch)
    outside = tmp_path / "runner-owned/plugin"
    outside.mkdir(parents=True)

    def run(args: list[str], **kwargs: object):
        return _result(
            args,
            stdout=(
                f"Installed plugin root: {outside}\n"
                if args[-3:] == ["plugin", "add", "tend-ci-runner@tend"]
                else ""
            ),
        )

    monkeypatch.setattr(codex_runner, "_run", run)

    assert codex_runner.main(["install-plugin"]) == 1


def test_install_plugin_rejects_ambiguous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, agent_home, _ = _set_sandbox_env(tmp_path, monkeypatch)
    plugin = agent_home / ".codex/plugins/tend-ci-runner"
    plugin.mkdir(parents=True)

    def run(args: list[str], **kwargs: object):
        line = f"Installed plugin root: {plugin}\n"
        return _result(
            args,
            stdout=(
                line * 2
                if args[-3:] == ["plugin", "add", "tend-ci-runner@tend"]
                else ""
            ),
        )

    monkeypatch.setattr(codex_runner, "_run", run)

    assert codex_runner.main(["install-plugin"]) == 1


def test_stage_agents_writes_as_the_sandbox_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action, agent_home, _ = _set_sandbox_env(tmp_path, monkeypatch)
    shared = action.parent / "shared"
    shared.mkdir()
    (shared / "system-prompt.md").write_text("Act as ${BOT_NAME}; keep $GH_TOKEN.\n")
    (action / "agents-tail.md").write_text("Look up $BOT_NAME.\n")
    monkeypatch.setenv("BOT_NAME", "tend-bot")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object):
        calls.append((args, kwargs))
        return _result(args)

    monkeypatch.setattr(codex_runner, "_run", run)

    assert codex_runner.main(["stage-agents"]) == 0

    agents = agent_home / ".codex/AGENTS.md"
    assert calls[0][0] == [
        "/usr/bin/sudo",
        "-u",
        "tend-sandbox",
        "/usr/bin/mkdir",
        "-p",
        str(agents.parent),
    ]
    assert calls[1][0][-2:] == ["/usr/bin/tee", str(agents)]
    assert calls[1][1]["input"] == (
        "# Tend CI guidance (Codex harness)\n\n"
        "Act as tend-bot; keep $GH_TOKEN.\n\n"
        "Look up tend-bot.\n"
    )


def test_run_withholds_runner_credentials_and_preserves_message_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, agent_home, _ = _set_sandbox_env(tmp_path, monkeypatch)
    run_dir = agent_home / "run"
    run_dir.mkdir()
    monkeypatch.setenv("GITHUB_TOKEN", "real-github-token")
    monkeypatch.setenv("GITHUB_ENV", "/runner/github-env")
    monkeypatch.setenv("GITHUB_ACTOR", "octocat")
    monkeypatch.setenv("OPENAI_API_KEY", "real-openai-key")
    monkeypatch.setenv("TEND_RUN_DIR", str(run_dir))
    monkeypatch.setenv("CODEX_PROXY_URL", "http://127.0.0.1:1234")
    monkeypatch.setenv("MODEL", "gpt-test")
    monkeypatch.setenv("EFFORT", "high")
    monkeypatch.setenv(
        "EXTRA_ARGS", "--skip-git-repo-check\n--config\nproject_doc_max_bytes=8192"
    )
    monkeypatch.setenv("PROMPT", "Review this")
    monkeypatch.setenv("BOT_NAME", "tend-bot")
    monkeypatch.setenv("BOT_ID", "123")
    monkeypatch.setenv("AUTH_MODE", "api-key")
    monkeypatch.setenv("TEND_INSIDE_SANDBOX", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object):
        calls.append((args, kwargs))
        if "/opt/codex/bin/codex" in args:
            return _result(args, returncode=7)
        return _result(args)

    monkeypatch.setattr(codex_runner, "_run", run)

    assert codex_runner.main(["run"]) == 7
    codex = next(args for args, _kwargs in calls if "/opt/codex/bin/codex" in args)
    assert codex[codex.index("--output-last-message") + 1] == str(
        run_dir / "codex-final-message.md"
    )
    launch, kwargs = next(
        (args, options) for args, options in calls if "/opt/codex/bin/codex" in args
    )
    assert kwargs["check"] is False
    assert "GITHUB_TOKEN=real-github-token" not in launch
    assert "GITHUB_ENV=/runner/github-env" not in launch
    assert "OPENAI_API_KEY=real-openai-key" not in launch
    assert "BOT_NAME=tend-bot" in launch
    assert "BOT_ID=123" in launch
    assert "NO_PROXY=" in launch
    assert "no_proxy=" in launch
    codex_at = launch.index("/opt/codex/bin/codex")
    assert launch[codex_at:] == [
        "/opt/codex/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "--config",
        "project_doc_max_bytes=8192",
        "--model",
        "gpt-test",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-last-message",
        str(run_dir / "codex-final-message.md"),
        "--config",
        (
            "model_providers.tend-openai={ name = 'Tend OpenAI proxy', "
            "base_url = 'http://127.0.0.1:1234/v1', wire_api = 'responses' }"
        ),
        "--config",
        'model_provider="tend-openai"',
        "--config",
        'shell_environment_policy.set.NO_PROXY="127.0.0.1,localhost"',
        "--config",
        'shell_environment_policy.set.no_proxy="localhost,127.0.0.1"',
        "--config",
        'cli_auth_credentials_store="file"',
        "--config",
        'model_reasoning_effort="high"',
        "Review this",
    ]


def test_run_uses_staged_subscription_auth_without_responses_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, agent_home, _ = _set_sandbox_env(tmp_path, monkeypatch)
    run_dir = agent_home / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TEND_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("PROMPT", "Review this")
    monkeypatch.setenv("AUTH_MODE", "subscription")
    monkeypatch.setenv("TEND_INSIDE_SANDBOX", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "also-configured")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args: list[str], **kwargs: object):
        calls.append((args, kwargs))
        if "/usr/bin/test" in args:
            return _result(args, returncode=1)
        return _result(args)

    monkeypatch.setattr(codex_runner, "_run", run)

    assert codex_runner.main(["run"]) == 0
    launch, kwargs = next(
        (args, options) for args, options in calls if "/opt/codex/bin/codex" in args
    )
    assert kwargs["check"] is False
    assert all(not item.startswith("OPENAI_API_KEY=") for item in launch)
    assert "NO_PROXY=" not in launch
    assert "no_proxy=" not in launch
    codex_at = launch.index("/opt/codex/bin/codex")
    assert launch[codex_at:] == [
        "/opt/codex/bin/codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-last-message",
        str(run_dir / "codex-final-message.md"),
        "--config",
        'cli_auth_credentials_store="file"',
        "Review this",
    ]


def test_run_refuses_to_create_a_second_execution_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_sandbox_env(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="only inside the SRT lifecycle"):
        codex_runner.main(["run"])
