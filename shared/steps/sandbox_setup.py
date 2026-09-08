"""Run adopter dependency setup inside the agent's SRT lifecycle.

``sandbox_setup:`` commands run immediately before the harness process, as the
same non-sudo user, with the same environment and disposable checkout. Their
on-disk effects therefore reach the agent without creating a second execution
boundary. ``setup:`` remains runner-owned and cannot populate this checkout.

Environment-only changes made by the command shell do not persist into the
harness. Use ``sandbox_path:`` and ``sandbox_env:`` for those; use
``sandbox_setup:`` for installs, cache warming, and generated files.
"""

from __future__ import annotations

import os
import subprocess

import _common

STEP = "sandbox-setup"


def setup_argv(commands: str) -> list[str]:
    """Build the one setup command executed inside the existing sandbox.

    The command inherits the environment SRT finalized for its network
    namespace. It is one ``bash -c`` argument rather than a temp file or stdin:
    no runner-owned file must cross the boundary, and an installer reading
    stdin cannot consume the remainder. ``-e`` makes a failed command stop the
    lifecycle before the harness starts.
    """
    return [
        "/usr/bin/bash",
        "-eo",
        "pipefail",
        "-c",
        commands,
    ]


def main() -> int:
    if os.environ.get("TEND_INSIDE_SANDBOX") != "1":
        raise RuntimeError("sandbox_setup may run only inside the SRT lifecycle")
    commands = os.environ.get("TEND_SANDBOX_SETUP", "")
    if not commands:
        return 0

    code = subprocess.run(setup_argv(commands), check=False).returncode
    if code == 0:
        _common.log(STEP, "ran adopter sandbox_setup commands inside SRT")
    return code


if __name__ == "__main__":
    _common.run(main)
