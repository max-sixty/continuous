"""The adopter's `sandbox_setup:` command inside the SRT lifecycle."""

from __future__ import annotations

import os
import subprocess

import pytest
import sandbox_setup


@pytest.fixture(autouse=True)
def _no_inherited_context(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("GITHUB_"):
            monkeypatch.delenv(name)


@pytest.fixture
def step_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEND_INSIDE_SANDBOX", "1")
    monkeypatch.delenv("TEND_SANDBOX_SETUP", raising=False)


def test_setup_commands_are_one_argument_in_the_existing_sandbox(
    step_env: None,
) -> None:
    assert sandbox_setup.setup_argv("uv sync") == [
        "/usr/bin/bash",
        "-eo",
        "pipefail",
        "-c",
        "uv sync",
    ]


def test_main_refuses_a_second_uid_boundary(
    step_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEND_INSIDE_SANDBOX")

    with pytest.raises(RuntimeError, match="only inside the SRT lifecycle"):
        sandbox_setup.main()


def test_empty_setup_is_a_noop(step_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("empty setup started a process"),
    )

    assert sandbox_setup.main() == 0


def test_failed_setup_stops_the_lifecycle(
    step_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEND_SANDBOX_SETUP", "false")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 3),
    )

    assert sandbox_setup.main() == 3


def test_successful_setup_is_reported(
    step_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TEND_SANDBOX_SETUP", "uv sync")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
    )

    assert sandbox_setup.main() == 0
    assert capsys.readouterr().out == (
        "[sandbox-setup] ran adopter sandbox_setup commands inside SRT\n"
    )
