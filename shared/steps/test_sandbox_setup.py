"""The adopter's `sandbox_setup:` commands and the reachability report.

The listing programs run for real against directories under `tmp_path`, as the
runner — the same program the sandbox side runs under `sudo`. What a test cannot
have is the second uid, so `sudo` calls are answered by a stand-in that records
the argv it was asked to run and hands back a canned listing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import sandbox_setup


def _bin(directory: Path, *names: str, mode: int = 0o755) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = directory / name
        path.write_text("#!/bin/sh\n")
        path.chmod(mode)
    return directory


@pytest.fixture(autouse=True)
def _no_inherited_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crossings compose `os.environ`, so the developer's shell must not."""
    for name in list(os.environ):
        if name.startswith("GITHUB_"):
            monkeypatch.delenv(name)


def test_executables_lists_what_this_uid_can_run(tmp_path: Path) -> None:
    """The X_OK test answers the question that matters on either side.

    Whether a uid can execute the file, not whether the file is there — a
    blocked shim and a real tool are both present; only one runs. A
    dot-prefixed name is not a command the shell that ran before this one
    would have matched, so it is not one this reports as missing either.
    """
    first = _bin(tmp_path / "first", "git", "jq")
    second = _bin(tmp_path / "second", "cargo", ".hidden")
    _bin(second, "not-executable", mode=0o644)
    (second / "a-directory").mkdir()

    path = os.pathsep.join([str(first), "", str(second), str(tmp_path / "absent")])

    assert sandbox_setup.executables(path) == {"git", "jq", "cargo"}


def test_a_listing_that_is_not_utf8_costs_only_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filename on either PATH need not be UTF-8, and this report is best-effort.

    Under the C locale the lister's own stdout carries such a name through as
    the raw bytes (PEP 540 turns on surrogateescape), so a strict decode here
    would raise out of a step whose whole job is to log what the agent cannot
    reach — before the agent has run at all.
    """
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"git\nx\xe9\n"),
    )

    assert sandbox_setup.executables("/usr/bin") == {"git", "x\ufffd"}


def test_executables_discards_the_output_of_a_failed_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed run must not be read as a listing, however much it printed.

    That is the whole reason the caller distinguishes empty from populated: the
    agent always resolves /usr/bin, so an empty agent-side listing means the
    call failed, and reporting from it would name every command the runner has.
    """
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "git\njq\n", "boom"),
    )

    assert sandbox_setup.executables(str(tmp_path), as_user="tend-sandbox") == set()


@dataclass
class FakeSandbox:
    """Answers `sudo -u <user>` with a canned listing; runs everything else."""

    real_run: Any
    listings: dict[str, list[str]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if argv[:2] != ["sudo", "-u"]:
            return self.real_run(argv, **kwargs)
        program = (
            "blocked" if argv[5] == sandbox_setup.RESOLVE_BLOCKED else "executables"
        )
        return subprocess.CompletedProcess(
            argv, 0, "\n".join(self.listings.get(program, [])).encode(), b""
        )

    def asked(self, program: str) -> list[str]:
        """The one sandbox-side call running *program*, as argv."""
        hits = [
            call
            for call in self.calls
            if call[:2] == ["sudo", "-u"] and call[5] == program
        ]
        assert len(hits) == 1, f"expected one sandbox-side call, got {hits}"
        return hits[0]


#: Distinct from anything the runner would have, so a call that asked the wrong
#: side about the wrong PATH is visible in the argv rather than plausible.
AGENT_PATH = "/usr/bin:/opt/sandbox/bin"

Report = Callable[..., tuple[str, "FakeSandbox"]]


@pytest.fixture
def report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Report:
    """Run the reachability report over a runner PATH under *tmp_path*."""

    def go(
        *,
        runner: list[str],
        agent: list[str],
        blocked: list[str] | None = None,
        blocked_path: str = "",
    ) -> tuple[str, FakeSandbox]:
        monkeypatch.setenv("PATH", str(_bin(tmp_path / "runner-path", *runner)))
        fake = FakeSandbox(
            subprocess.run, {"executables": agent, "blocked": blocked or []}
        )
        monkeypatch.setattr(subprocess, "run", fake)
        sandbox_setup.report_reachability(
            sandbox="tend-sandbox", agent_path=AGENT_PATH, blocked_path=blocked_path
        )
        return capsys.readouterr().out, fake

    return go


def test_report_names_only_what_the_agent_cannot_reach(report: Report) -> None:
    """Shared system paths cross the uid boundary; runner-home paths do not.

    Reported, not fatal: only the adopter knows which tools its gate needs.
    """
    out, fake = report(runner=["git", "jq", "tend-probe"], agent=["git", "jq"])

    assert out.splitlines()[0] == (
        "[sandbox-setup] on the runner's PATH, unavailable to the agent: tend-probe"
    )
    assert "install it as the sandbox user with sandbox_setup:" in out
    asked = fake.asked(sandbox_setup.LIST_EXECUTABLES)
    assert asked[2:4] == ["tend-sandbox", "/usr/bin/python3"], (
        "the far side must be asked as the sandbox uid, through an interpreter "
        "that uid can execute — `sys.executable` may be an adopter's "
        "actions/setup-python under the runner's home. Named literally here: "
        "against the constant, this assertion pins nothing"
    )
    assert asked[6] == AGENT_PATH, (
        "the sandbox was asked about the wrong PATH; the runner's own would "
        "make every command look reachable"
    )


def test_report_says_nothing_when_every_command_crosses(report: Report) -> None:
    out, _ = report(runner=["git"], agent=["git", "jq"])

    assert out == ""


def test_report_stands_down_when_the_agent_side_listing_failed(
    report: Report,
) -> None:
    """An empty agent side is a failed call, not an agent with no commands."""
    out, _ = report(runner=["git", "jq"], agent=[])

    assert out == (
        "[sandbox-setup] could not list the agent's PATH; no reachability report\n"
    ), "a failed listing named every command the runner has"


def test_report_adds_back_a_blocker_the_agent_still_resolves(
    report: Report, tmp_path: Path
) -> None:
    """A blocker is itself executable, so the name diff alone cannot see it.

    Home-selected commands get a failure shim ahead of the shared fallback so
    they cannot silently change version; the shim resolves under both uids, so
    only the resolution says the name is unusable.
    """
    blocked_path = str(_bin(tmp_path / "blocked", "tend-probe"))
    out, fake = report(
        runner=["git", "tend-probe"],
        agent=["git", "tend-probe"],
        blocked=["tend-probe"],
        blocked_path=blocked_path,
    )

    assert out.splitlines()[0].endswith(": tend-probe")
    asked = fake.asked(sandbox_setup.RESOLVE_BLOCKED)
    assert asked[2:4] == ["tend-sandbox", "/usr/bin/python3"]
    assert asked[6:] == [AGENT_PATH, blocked_path]


def test_blocked_shims_needs_a_directory_to_look_in(tmp_path: Path) -> None:
    """An unset or absent TEND_BLOCKED_PATH costs the report nothing."""
    assert sandbox_setup.blocked_shims("/usr/bin", "", as_user="s") == set()
    assert (
        sandbox_setup.blocked_shims("/usr/bin", str(tmp_path / "gone"), as_user="s")
        == set()
    )


def test_a_shim_is_blocked_only_while_nothing_on_the_path_precedes_it(
    tmp_path: Path,
) -> None:
    """The program itself, run as this uid: PATH order decides, name by name."""
    blocked = _bin(tmp_path / "blocked", "tend-probe", "tend-gone")
    local = _bin(tmp_path / "local", "tend-probe")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            sandbox_setup.RESOLVE_BLOCKED,
            os.pathsep.join([str(local), str(blocked)]),
            str(blocked),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.split() == ["tend-gone"], (
        "a shim an earlier PATH entry replaced is reachable, not blocked"
    )


def test_setup_commands_cross_as_one_bash_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a temp file (no sandbox-side read of a runner-owned path) and not
    stdin (a setup command that reads stdin can't swallow the remaining lines
    and exit 0). `-e` inside so a failure fails the step loudly.
    """
    agent_env = tmp_path / "agent-env"
    agent_env.write_text("HOME=/sandbox\n")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")

    argv = sandbox_setup.setup_argv(
        "apt-get install -y tend-probe",
        sandbox="tend-sandbox",
        agent_env_file=str(agent_env),
    )

    assert argv == [
        "sudo",
        "-u",
        "tend-sandbox",
        "env",
        "HOME=/sandbox",
        "GITHUB_EVENT_NAME=schedule",
        "bash",
        "-eo",
        "pipefail",
        "-c",
        "apt-get install -y tend-probe",
    ]


@pytest.fixture
def step_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_env = tmp_path / "agent-env"
    agent_env.write_text("HOME=/sandbox\n")
    monkeypatch.setenv("SANDBOX", "tend-sandbox")
    monkeypatch.setenv("AGENT_ENV_FILE", str(agent_env))
    monkeypatch.setenv("AGENT_PATH", "/usr/bin")
    monkeypatch.delenv("TEND_SANDBOX_SETUP", raising=False)
    monkeypatch.delenv("TEND_BLOCKED_PATH", raising=False)


def test_main_carries_a_failed_setup_command_out_as_the_step_status(
    step_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing `sandbox_setup:` command fails the step rather than reaching the run.

    The report is skipped with it: what it would describe is a sandbox that
    never finished being set up.
    """
    monkeypatch.setenv("TEND_SANDBOX_SETUP", "false")
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 3)
    )

    assert sandbox_setup.main() == 3
    assert capsys.readouterr().out == ""


def test_main_reports_reachability_with_no_setup_commands_configured(
    step_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty `sandbox_setup:` is exactly the config a missing tool hides in."""
    monkeypatch.setenv("PATH", str(_bin(tmp_path / "runner-path", "git", "cargo")))
    fake = FakeSandbox(subprocess.run, {"executables": ["git"]})
    monkeypatch.setattr(subprocess, "run", fake)

    assert sandbox_setup.main() == 0
    assert "unavailable to the agent: cargo" in capsys.readouterr().out
    assert not any("bash" in argv for argv in fake.calls), (
        "no setup commands were configured, so nothing may run in the sandbox"
    )
