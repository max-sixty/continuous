# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Delete the event checkout and per-run runtime after the sandbox is reaped."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

WORKSPACE_CONTAINER = re.compile(r"tend-agent-workspace-[A-Za-z0-9._-]+\Z")
RUNTIME_CONTAINER = re.compile(r"tend-runtime\.[A-Za-z0-9]+\Z")


def fail(message: str) -> int:
    print(f"::error::{message}", flush=True)
    return 1


def workspace_container(workspace: Path) -> Path:
    """Return the dedicated /tmp container or reject a broader target."""
    if workspace.name != "checkout":
        raise ValueError("TEND_AGENT_WORKSPACE must end in /checkout")
    container = workspace.parent
    if container.parent != Path("/tmp") or not WORKSPACE_CONTAINER.fullmatch(
        container.name
    ):
        raise ValueError(
            "TEND_AGENT_WORKSPACE must be inside a tend-agent-workspace-* /tmp container"
        )
    return container


def runtime_container(runtime: Path) -> Path:
    """Accept only the mktemp shape used by install-sandbox-runtime.sh."""
    if runtime.parent != Path("/tmp") or not RUNTIME_CONTAINER.fullmatch(runtime.name):
        raise ValueError("TEND_RUNTIME_ROOT must be a tend-runtime.* /tmp directory")
    return runtime


def targets() -> list[Path]:
    """Resolve every resource that this run got far enough to create."""
    resources: list[Path] = []
    workspace = os.environ.get("TEND_AGENT_WORKSPACE", "")
    if workspace:
        resources.append(workspace_container(Path(workspace)))
    runtime = os.environ.get("TEND_RUNTIME_ROOT", "")
    if runtime:
        resources.append(runtime_container(Path(runtime)))
    return resources


def main() -> int:
    try:
        resources = targets()
        if not resources:
            return 0
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
                    "refusing to dispose resources while sandbox processes live"
                )
            if live.returncode != 1:
                return fail("could not verify that sandbox processes were reaped")
        subprocess.run(
            [
                "/usr/bin/sudo",
                "/usr/bin/rm",
                "-rf",
                "--",
                *(str(resource) for resource in resources),
            ],
            check=True,
        )
        remaining = [
            str(resource) for resource in resources if os.path.lexists(resource)
        ]
        if remaining:
            return fail(
                f"sandbox resources still exist after cleanup: {', '.join(remaining)}"
            )
        return 0
    except (OSError, subprocess.CalledProcessError, ValueError) as problem:
        return fail(str(problem))


if __name__ == "__main__":
    raise SystemExit(main())
