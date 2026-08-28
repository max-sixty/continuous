"""Finishes preparing the sandbox, immediately before the agent runs.

Executes the adopter's ``sandbox_setup:`` commands (from ``.config/tend.yaml``,
threaded in as ``TEND_SANDBOX_SETUP``) INSIDE the sandbox, as the non-sudo
sandbox user, then reports which commands the runner can resolve and the agent
cannot.

``sandbox_setup:`` is the general lever runner-side ``setup:`` cannot provide:
``setup:`` runs as the runner user around the composite action, while these
commands run with the same launch env the agent gets (``$AGENT_ENV_FILE``:
proxy routing, CA trust, dummy credentials, plus any ``sandbox_path`` /
``sandbox_env`` additions) and with the workspace as the working directory.

Env-only tweaks (PATH, exported vars) do NOT persist to the agent from here — a
child shell's exports die with it. Use ``sandbox_path:`` / ``sandbox_env:`` for
those; use ``sandbox_setup:`` for actions with on-disk effects (installing a
tool, warming a cache, generating a file).

Inputs (env): ``TEND_SANDBOX_SETUP`` (the commands; empty → the report only),
``SANDBOX``, ``AGENT_ENV_FILE``, ``AGENT_PATH`` and ``TEND_BLOCKED_PATH``
(exported by ``proxy/setup-sandbox.sh`` via ``$GITHUB_ENV``), plus the
``GITHUB_*`` context from Actions. Used by the Claude harness action.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import _common
import _sandbox

STEP = "sandbox-setup"

#: The sandbox side runs this, so it names an interpreter that crosses the uid
#: boundary. ``sys.executable`` need not: an adopter's ``setup:`` is free to run
#: ``actions/setup-python``, whose interpreter sits under the runner's home,
#: which is precisely what this step exists to report as unreachable.
SANDBOX_PYTHON = "/usr/bin/python3"

#: Executable names on the PATH given as ``argv[1]``, one per line.
#:
#: Run on both sides of the uid boundary, so the ``X_OK`` test answers the
#: question that matters: can THAT uid execute it, not does the file exist. A
#: directory the lister cannot read contributes nothing, so its commands read as
#: missing on that side. That is a false positive the diff cannot distinguish
#: from a real one; PATH directories are public on hosted runners.
#:
#: Dot-prefixed names are skipped because the shell that ran before this one
#: skipped them: ``for f in "$dir"/*`` never matches them, and a hidden file in
#: a PATH directory is not a command anyone types.
LIST_EXECUTABLES = r"""
import os, sys

names = set()
for directory in sys.argv[1].split(os.pathsep):
    if not directory:
        continue
    try:
        entries = list(os.scandir(directory))
    except OSError:
        continue
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_file() and os.access(entry.path, os.X_OK):
            names.add(entry.name)
print("\n".join(sorted(names)))
"""

#: Names in the blocked-shim directory (``argv[2]``) that still resolve to a
#: shim on the PATH (``argv[1]``), one per line.
#:
#: A blocker is itself executable, so the name diff already sees it. Add it back
#: only while no earlier ``sandbox_path`` / ``.local/bin`` command has replaced
#: it. Runs as the sandbox user: whose PATH resolves to what is the question.
RESOLVE_BLOCKED = r"""
import os, sys

path, blocked = sys.argv[1], sys.argv[2]
directories = [d for d in path.split(os.pathsep) if d]
try:
    shims = sorted(os.listdir(blocked))
except OSError:
    shims = []
for name in shims:
    for directory in directories:
        if directory == blocked:
            print(name)
            break
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            break
"""


def _lines(argv: list[str]) -> list[str]:
    """*argv*'s stdout as lines; a failed run reports nothing rather than lying.

    Only stdout is captured: the listing's stderr goes to the step log, so a
    failure on the far side of the boundary says why rather than vanishing
    behind "could not list the agent's PATH".

    Decoded with ``errors="replace"`` rather than through ``text=True``: a
    filename on either PATH need not be UTF-8, and a strict decode would raise
    out of a report that is best-effort by design.
    """
    result = subprocess.run(argv, stdout=subprocess.PIPE)
    if result.returncode != 0:
        return []
    return result.stdout.decode("utf-8", errors="replace").splitlines()


def executables(path_value: str, *, as_user: str | None = None) -> set[str]:
    """The executable names on *path_value*, listed by *as_user*'s uid."""
    program = [SANDBOX_PYTHON if as_user else sys.executable, "-c", LIST_EXECUTABLES]
    prefix = ["sudo", "-u", as_user] if as_user else []
    return set(_lines([*prefix, *program, path_value]))


def blocked_shims(path_value: str, blocked_path: str, *, as_user: str) -> set[str]:
    """The blocked-shim names in *blocked_path* that *as_user* still resolves to."""
    if not blocked_path or not Path(blocked_path).is_dir():
        return set()
    argv = ["sudo", "-u", as_user, SANDBOX_PYTHON, "-c", RESOLVE_BLOCKED]
    return set(_lines([*argv, path_value, blocked_path]))


def setup_argv(commands: str, *, sandbox: str, agent_env_file: str) -> list[str]:
    """The command that runs the adopter's ``sandbox_setup:`` block in the sandbox.

    The commands go through ``bash -c``'s argument: no temp file (so no
    sandbox-side read permission on a runner-owned path), and not stdin (so a
    setup command that reads stdin — an installer prompt, ``read`` — can't
    swallow the remaining lines and exit 0). ``-e`` inside so a failing setup
    command fails the step loudly rather than silently proceeding to the run.

    ``sudo env`` replaces the environment with only what is listed, so the
    launch env is rebuilt from :func:`_sandbox.launch_env` — the same
    composition the agent gets, ``GITHUB_*`` context included: one
    ``sandbox_setup:`` block runs for every workflow and event, and a command
    scopes itself with ``$GITHUB_WORKFLOW`` or ``$GITHUB_EVENT_NAME``.
    """
    return [
        "sudo",
        "-u",
        sandbox,
        "env",
        *_sandbox.launch_env(agent_env_file),
        "bash",
        "-eo",
        "pipefail",
        "-c",
        commands,
    ]


def report_reachability(*, sandbox: str, agent_path: str, blocked_path: str) -> None:
    """Log what the runner's PATH resolves and the agent's does not.

    Shared system/toolcache paths cross the uid boundary; runner-home paths do
    not. Home-selected commands have failure shims before shared paths so they
    cannot silently change version; ``sandbox_setup`` can shadow a shim from
    ``.local/bin``. Reported, not fatal: only the adopter knows which tools its
    gate needs.
    """
    agent = executables(agent_path, as_user=sandbox)
    if not agent:
        # Nothing on the agent's side means the listing failed, not that the
        # agent has no commands — it always resolves /usr/bin. Reporting then
        # would name every command the runner has.
        _common.log(STEP, "could not list the agent's PATH; no reachability report")
        return

    runner = executables(os.environ.get("PATH", ""))
    unavailable = sorted(
        (runner - agent) | blocked_shims(agent_path, blocked_path, as_user=sandbox)
    )
    if unavailable:
        _common.log(
            STEP,
            "on the runner's PATH, unavailable to the agent: " + " ".join(unavailable),
        )
        _common.log(
            STEP,
            "if the session needs one of those, install it as the sandbox user "
            "with sandbox_setup: in .config/tend.yaml",
        )


def main() -> int:
    env = _common.require_env("SANDBOX", "AGENT_ENV_FILE", "AGENT_PATH")
    sandbox = env["SANDBOX"]

    commands = os.environ.get("TEND_SANDBOX_SETUP", "")
    if commands:
        argv = setup_argv(
            commands, sandbox=sandbox, agent_env_file=env["AGENT_ENV_FILE"]
        )
        code = subprocess.run(argv).returncode
        if code != 0:
            return code
        _common.log(STEP, f"ran adopter sandbox_setup commands as {sandbox}")

    report_reachability(
        sandbox=sandbox,
        agent_path=env["AGENT_PATH"],
        blocked_path=os.environ.get("TEND_BLOCKED_PATH", ""),
    )
    return 0


if __name__ == "__main__":
    _common.run(main)
