from __future__ import annotations

import subprocess
from pathlib import Path

import dispose_sandbox_resources as dispose
import pytest


def result(args: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode)


def test_accepts_only_the_dedicated_workspace_shape() -> None:
    expected = Path("/tmp/tend-agent-workspace-abc123")
    assert dispose.workspace_container(expected / "checkout") == expected

    for path in (
        Path("/tmp/tend-agent-workspace-abc123"),
        Path("/tmp/unrelated/checkout"),
        Path("/var/tmp/tend-agent-workspace-abc123/checkout"),
    ):
        with pytest.raises(ValueError):
            dispose.workspace_container(path)


def test_accepts_only_the_dedicated_runtime_shape() -> None:
    expected = Path("/tmp/tend-runtime.aB123z")
    assert dispose.runtime_container(expected) == expected

    for path in (
        Path("/tmp/tend-runtime"),
        Path("/tmp/tend-runtime.bad/name"),
        Path("/var/tmp/tend-runtime.abc123"),
    ):
        with pytest.raises(ValueError):
            dispose.runtime_container(path)


def test_disposes_only_after_the_sandbox_uid_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Path("/tmp/tend-agent-workspace-abc123/checkout")
    monkeypatch.setenv("TEND_AGENT_WORKSPACE", str(workspace))
    monkeypatch.setenv("TEND_RUNTIME_ROOT", "/tmp/tend-runtime.r1a2b3")
    monkeypatch.setenv("SANDBOX", "tend-sandbox")
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return result(args, returncode=1 if args[0] == "/usr/bin/pgrep" else 0)

    monkeypatch.setattr(dispose.subprocess, "run", run)
    monkeypatch.setattr(dispose.os.path, "lexists", lambda _path: False)

    assert dispose.main() == 0
    assert calls == [
        ["/usr/bin/pgrep", "-u", "tend-sandbox"],
        [
            "/usr/bin/sudo",
            "/usr/bin/rm",
            "-rf",
            "--",
            "/tmp/tend-agent-workspace-abc123",
            "/tmp/tend-runtime.r1a2b3",
        ],
    ]


def test_refuses_to_dispose_while_a_sandbox_process_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEND_AGENT_WORKSPACE", "/tmp/tend-agent-workspace-abc123/checkout"
    )
    monkeypatch.setenv("SANDBOX", "tend-sandbox")
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return result(args)

    monkeypatch.setattr(dispose.subprocess, "run", run)

    assert dispose.main() == 1
    assert calls == [["/usr/bin/pgrep", "-u", "tend-sandbox"]]


def test_disposes_partial_runtime_when_workspace_was_never_prepared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEND_AGENT_WORKSPACE", raising=False)
    monkeypatch.setenv("TEND_RUNTIME_ROOT", "/tmp/tend-runtime.abc123")
    monkeypatch.delenv("SANDBOX", raising=False)
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return result(args)

    monkeypatch.setattr(dispose.subprocess, "run", run)
    monkeypatch.setattr(dispose.os.path, "lexists", lambda _path: False)

    assert dispose.main() == 0
    assert calls == [
        [
            "/usr/bin/sudo",
            "/usr/bin/rm",
            "-rf",
            "--",
            "/tmp/tend-runtime.abc123",
        ]
    ]
