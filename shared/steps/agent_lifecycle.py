"""Run setup and one harness inside a single Sandbox Runtime boundary."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import sandbox_setup


def probe_boundary() -> None:
    """Fail unless SRT's Linux seccomp and read boundary are effective."""
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except PermissionError:
        pass
    else:
        raise RuntimeError("SRT capability probe created an AF_UNIX socket")

    runner_workspace = Path(os.environ["TEND_RUNNER_WORKSPACE"])
    try:
        (runner_workspace / ".git/config").read_bytes()
    except OSError:
        pass
    else:
        raise RuntimeError("SRT capability probe read the runner checkout")

    agent_workspace = Path(os.environ["TEND_AGENT_WORKSPACE"])
    if not (agent_workspace / ".git/config").is_file():
        raise RuntimeError("SRT capability probe cannot read the agent checkout")

    probe_url = os.environ.get("TEND_BOUNDARY_PROBE_URL")
    if probe_url:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--fail",
                "--silent",
                "--show-error",
                "--proxy",
                os.environ["HTTP_PROXY"],
                "--noproxy",
                "",
                probe_url,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode or result.stdout != "tend-srt-network-ok\n":
            raise RuntimeError("SRT capability probe did not traverse the HTTP broker")

    probe_executable = os.environ.get("TEND_BOUNDARY_PROBE_EXECUTABLE")
    if probe_executable:
        result = subprocess.run(
            [probe_executable], check=False, capture_output=True, text=True
        )
        if result.returncode or result.stdout != "tend-srt-tool-ok\n":
            raise RuntimeError("SRT capability probe cannot execute the harness tool")


def main() -> int:
    probe_boundary()
    os.environ["TEND_INSIDE_SANDBOX"] = "1"
    setup_code = sandbox_setup.main()
    if setup_code:
        return setup_code

    harness = os.environ.get("TEND_HARNESS", "")
    if harness == "probe":
        return 0
    if harness == "claude":
        import run_claude

        return run_claude.main()
    if harness == "codex":
        runner = Path(os.environ["TEND_CODEX_RUNNER"])
        return subprocess.run(
            ["/usr/bin/python3", "-E", "-s", str(runner), "run"], check=False
        ).returncode
    raise ValueError(f"unknown TEND_HARNESS: {harness or '<unset>'}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as problem:
        print(f"agent lifecycle: {problem}", file=sys.stderr)
        raise SystemExit(1) from None
