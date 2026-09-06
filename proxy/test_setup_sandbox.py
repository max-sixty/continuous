"""Unit coverage for sandbox setup policy that does not need a second UID."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest
import setup_sandbox


def _paths(tmp_path: Path) -> setup_sandbox.Paths:
    runner_home = tmp_path / "runner"
    workspace = runner_home / "work/repo"
    workspace.mkdir(parents=True)
    runner_temp = tmp_path / "temp"
    runner_temp.mkdir()
    action = tmp_path / "action"
    action.mkdir()
    uv = tmp_path / "uv"
    uv.mkdir()
    return setup_sandbox.Paths(
        workspace=workspace.resolve(),
        runner_temp=runner_temp.resolve(),
        action_path=action.resolve(),
        tend_uv_dir=uv.resolve(),
        github_env=tmp_path / "github-env",
        runner_home=runner_home.resolve(),
    )


def test_configured_path_accepts_the_checkout_and_refuses_the_runner_home(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)

    assert setup_sandbox.configured_paths(
        str(paths.workspace / "bin"), paths=paths
    ) == [str(paths.workspace / "bin")]
    with pytest.raises(ValueError, match="under the runner's home"):
        setup_sandbox.configured_paths(str(paths.runner_home / "bin"), paths=paths)


def test_workspace_path_keeps_precedence_over_runner_home_rewrite(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    workspace_bin = paths.workspace / "bin"
    workspace_bin.mkdir()

    plan = setup_sandbox.plan_agent_path(
        runner_tool_path=str(workspace_bin),
        extras=[],
        paths=paths,
        can_execute=lambda _: False,
    )

    assert str(workspace_bin) in plan.agent_path
    assert plan.dropped_home_paths == []


def test_agent_uv_fallback_trails_adopter_paths_and_needs_no_blocker(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    runner_bin = paths.runner_home / "bin"
    runner_bin.mkdir()
    for name in ("uv", "uvx", "tend-probe"):
        executable = runner_bin / name
        executable.touch(mode=0o755)
    adopter_bin = tmp_path / "adopter-bin"

    plan = setup_sandbox.plan_agent_path(
        runner_tool_path=str(runner_bin),
        extras=[str(adopter_bin)],
        paths=paths,
        can_execute=lambda _: False,
    )

    assert plan.agent_path[0] == str(adopter_bin)
    assert plan.agent_path[-1] == str(setup_sandbox.TEND_AGENT_UV_DIR)
    assert plan.blocked_commands == ["tend-probe"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("PATH=/tmp/bin", "reserved key 'PATH'"),
        ("NOT_AN_ASSIGNMENT", "not NAME=VALUE"),
    ],
)
def test_adopter_environment_rejects_unsafe_records(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        setup_sandbox.adopter_env(raw)


def test_adopter_environment_preserves_values_after_the_first_equals() -> None:
    assert setup_sandbox.adopter_env("TEND_VALUE=a=b\n") == ["TEND_VALUE=a=b"]


def test_every_fixed_agent_assignment_is_reserved() -> None:
    assignments = setup_sandbox.base_agent_env(
        "/usr/bin", ("ANTHROPIC_API_KEY", "dummy")
    )
    assert {line.split("=", 1)[0] for line in assignments} <= (
        setup_sandbox.RESERVED_SANDBOX_ENV
    )


def test_github_only_agent_environment_has_no_model_credential() -> None:
    assignments = setup_sandbox.base_agent_env("/usr/bin", None)

    assert not any(
        line.startswith(("ANTHROPIC_API_KEY=", "CLAUDE_CODE_OAUTH_TOKEN="))
        for line in assignments
    )
    assert "OPENAI_API_KEY" in setup_sandbox.RESERVED_SANDBOX_ENV
    assert "CODEX_API_KEY" in setup_sandbox.RESERVED_SANDBOX_ENV
    assert "CODEX_AUTH_JSON" in setup_sandbox.RESERVED_SANDBOX_ENV
    assert "CODEX_HOME" in setup_sandbox.RESERVED_SANDBOX_ENV


def test_proxy_uvx_isolated_from_adopter_python_and_uv_configuration(
    tmp_path: Path,
) -> None:
    command = setup_sandbox.uvx_command(
        _paths(tmp_path), version="1.2.3", args=["--version"]
    )

    assert command[1:6] == [
        "--no-config",
        "--no-python-downloads",
        "--python",
        "/usr/bin/python3",
        "--from",
    ]
    assert command[-3:] == ["mitmproxy==1.2.3", "mitmdump", "--version"]


def test_runner_home_does_not_trust_an_empty_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "")

    assert (
        setup_sandbox.runner_home() == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    )
