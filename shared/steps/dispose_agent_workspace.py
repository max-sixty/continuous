# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Delete the disposable event checkout after the sandbox process tree is gone."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

CONTAINER_NAME = re.compile(r"tend-agent-workspace-[A-Za-z0-9._-]+\Z")


def fail(message: str) -> int:
    print(f"::error::{message}", flush=True)
    return 1


def workspace_container(workspace: Path) -> Path:
    """Return the dedicated /tmp container or reject a broader target."""
    if workspace.name != "checkout":
        raise ValueError("TEND_AGENT_WORKSPACE must end in /checkout")
    container = workspace.parent
    if container.parent != Path("/tmp") or not CONTAINER_NAME.fullmatch(container.name):
        raise ValueError(
            "TEND_AGENT_WORKSPACE must be inside a tend-agent-workspace-* /tmp container"
        )
    return container


def main() -> int:
    value = os.environ.get("TEND_AGENT_WORKSPACE", "")
    if not value:
        return 0
    try:
        container = workspace_container(Path(value))
        sandbox = os.environ.get("SANDBOX", "")
        if sandbox:
            live = subprocess.run(
                ["/usr/bin/pgrep", "-u", sandbox],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if live.returncode == 0:
                return fail(
                    "refusing to dispose workspace while sandbox processes live"
                )
            if live.returncode != 1:
                return fail("could not verify that sandbox processes were reaped")
        subprocess.run(
            ["/usr/bin/sudo", "/usr/bin/rm", "-rf", "--", str(container)],
            check=True,
        )
        if os.path.lexists(container):
            return fail("disposable agent workspace still exists after cleanup")
        return 0
    except (OSError, subprocess.CalledProcessError, ValueError) as problem:
        return fail(str(problem))


if __name__ == "__main__":
    raise SystemExit(main())
