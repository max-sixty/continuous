"""The environment the sandbox user is launched with.

The denylist is the only thing keeping the real PAT and the runner's own
command-file paths out of a uid that runs adopter code, and
``proxy/test-setup-sandbox.sh`` represents all five withheld paths with
``GITHUB_ENV`` alone — so drop one of the other four from :data:`WITHHELD` and
that suite still passes. These pin every name on both sides, and the order the
two halves are composed in.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import _sandbox
import pytest

WITHHELD = (
    "GITHUB_TOKEN",
    "GITHUB_ENV",
    "GITHUB_PATH",
    "GITHUB_OUTPUT",
    "GITHUB_STATE",
    "GITHUB_STEP_SUMMARY",
)

Compose = Callable[..., list[str]]


@pytest.fixture
def compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Compose:
    """`launch_env` given exactly *env* as the GitHub context, and *env_file*."""

    def build(env: dict[str, str], env_file: str = "") -> list[str]:
        for name in list(os.environ):
            if name.startswith("GITHUB_"):
                monkeypatch.delenv(name)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        path = tmp_path / "agent-env"
        path.write_text(env_file)
        return _sandbox.launch_env(path)

    return build


def test_launch_env_puts_the_context_after_the_file(compose: Compose) -> None:
    """The order `launch_env`'s docstring makes its postcondition, pinned."""
    pairs = compose(
        {"GITHUB_WORKFLOW": "tend-weekly"},
        env_file="HOME=/sandbox\nGITHUB_WORKFLOW=spoofed-by-sandbox-env\n",
    )

    assert pairs == [
        "HOME=/sandbox",
        "GITHUB_WORKFLOW=spoofed-by-sandbox-env",
        "GITHUB_WORKFLOW=tend-weekly",
    ]


def test_launch_env_withholds_every_denied_name(compose: Compose) -> None:
    """Each of the six is dropped, and dropping it is the only thing that is."""
    carried = {
        "GITHUB_WORKFLOW": "tend-weekly",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_REPOSITORY": "max-sixty/tend",
    }

    pairs = compose({**carried, **{name: f"secret-{name}" for name in WITHHELD}})

    assert sorted(pairs) == sorted(f"{k}={v}" for k, v in carried.items())


def test_launch_env_carries_a_name_the_denylist_never_heard_of(
    compose: Compose,
) -> None:
    """A denylist, not an allowlist: a GITHUB_* Actions adds later crosses.

    Losing this is the failure that hides — the agent and the setup commands
    would each be missing a name nobody notices until a skill reaches for it.
    """
    assert compose({"GITHUB_A_NAME_FROM_2030": "value"}) == [
        "GITHUB_A_NAME_FROM_2030=value"
    ]


def test_launch_env_is_anchored_to_the_prefix(compose: Compose) -> None:
    """`GITHUB_`, so a name that merely contains it stays on the runner.

    `MY_GITHUB_TOKEN` and `GITHUBBER_TOKEN` are the shapes that matter: an
    adopter `setup:` step is free to export either, and neither may ride across
    on a prefix the match got wrong at one end or the other.
    """
    pairs = compose(
        {
            "MY_GITHUB_TOKEN": "real",
            "GITHUBBER_TOKEN": "also-real",
            "NOT_GITHUB": "x",
            "GITHUB_ACTOR": "someone",
        }
    )

    assert pairs == ["GITHUB_ACTOR=someone"]


def test_launch_env_reads_the_file_as_the_shell_wrote_it(compose: Compose) -> None:
    """One element per newline-delimited record, and no phantom trailing one.

    `str.splitlines` would also break on \\v, \\f and U+2028 — characters a
    carried value may hold and the file's own framing does not — turning one
    assignment into two arguments, the second of them junk. Universal-newline
    translation on the read does the same for `\\r`, which `sandbox_env:` does
    not reject: `env` runs a trailing argument that is not an assignment as the
    command, so the split turns a stray carriage return into an exec.
    """
    assert compose({}, env_file="A=1\nB=two\vlines\n") == ["A=1", "B=two\vlines"]
    assert compose({}, env_file="FOO=a\rb\n") == ["FOO=a\rb"]
    assert compose({}, env_file="") == []


def test_launch_env_carries_a_value_that_is_not_utf_8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell that wrote the file was byte transparent, so this must be too.

    A non-UTF-8 byte in an adopter's `sandbox_env:` value would otherwise fail
    the step before the launch. Asserted through a real subprocess, because the
    round trip is `subprocess`'s `os.fsencode`, not anything this module does.
    """
    path = tmp_path / "agent-env"
    path.write_bytes(b"TEND_X=raw\xe9byte\n")

    pairs = _sandbox.launch_env(path)

    echo = "import os, sys; sys.stdout.buffer.write(os.fsencode(sys.argv[1]))"
    echoed = subprocess.run(
        [sys.executable, "-c", echo, pairs[0]], capture_output=True, check=True
    )
    assert echoed.stdout == b"TEND_X=raw\xe9byte"
