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
import run_claude
import sandbox_setup

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


# Both adopter-code crossings compose the GitHub context the same way: through
# `launch_env`, with tend's own assignments after it. `env` takes the final
# assignment of a name, so a GITHUB_*-named one appended by either caller would
# displace the real context — the failure this pins, scoped to the single argv
# rather than to file order, because "later in this argv" is what decides who
# wins.
CROSSING_CONTEXT = {"GITHUB_WORKFLOW": "tend-weekly", "GITHUB_ACTOR": "someone"}


def _crossings(agent_env_file: str) -> dict[str, list[str]]:
    return {
        "run_claude.launch_argv": run_claude.launch_argv(
            sandbox="tend-sandbox",
            agent_env_file=agent_env_file,
            model="m",
            allowed_tools="Bash",
            system_prompt="s",
            prompt="p",
            subprocess_env_scrub="0",
            bot_name="bot",
            bot_id="1",
            ci="true",
        ),
        "sandbox_setup.setup_argv": sandbox_setup.setup_argv(
            "echo hi", sandbox="tend-sandbox", agent_env_file=agent_env_file
        ),
    }


def test_every_crossing_carries_the_context_and_lets_nothing_displace_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith("GITHUB_"):
            monkeypatch.delenv(name)
    for name, value in CROSSING_CONTEXT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GITHUB_TOKEN", "the-real-pat")
    env_file = tmp_path / "agent-env"
    env_file.write_text("HOME=/sandbox\nGITHUB_TOKEN=dummy\n")

    for name, argv in _crossings(str(env_file)).items():
        assert argv[:4] == ["sudo", "-u", "tend-sandbox", "env"], name
        assert "GITHUB_TOKEN=the-real-pat" not in argv, (
            f"{name}: the runner's real PAT crossed into the sandbox"
        )
        for key, value in CROSSING_CONTEXT.items():
            pair = f"{key}={value}"
            assert pair in argv, f"{name}: the crossing dropped {key}"
            after = argv[argv.index(pair) + 1 :]
            assert not [a for a in after if a.startswith(f"{key}=")], (
                f"{name}: a later assignment displaces the real {key}: {after}"
            )
