"""Trusted outer supervisor for one SRT-contained Tend lifecycle."""

from __future__ import annotations

import base64
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

import _sandbox
from _safe_files import read_regular_nofollow

PASSTHROUGH = {
    "ACTION_PATH",
    "AGENT_ENV_FILE",
    "AGENT_HOME",
    "AGENT_PATH",
    "AUTH_MODE",
    "BOT_ID",
    "BOT_NAME",
    "CI",
    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
    "CODEX_BIN",
    "CODEX_PROXY_URL",
    "EFFORT",
    "EXTRA_ARGS",
    "MODEL",
    "NODE_BIN",
    "PROMPT",
    "SANDBOX",
    "SHOW_FULL_OUTPUT",
    "TEND_AGENT_WORKSPACE",
    "TEND_ALLOWED_TOOLS",
    "TEND_ARGS",
    "TEND_AUTO_MEMORY_SETTINGS",
    "TEND_AUTO_MEMORY_DIRECTORY",
    "TEND_BLOCKED_PATH",
    "TEND_BOUNDARY_PROBE_URL",
    "TEND_BOUNDARY_PROBE_EXECUTABLE",
    "TEND_CODEX_RUNNER",
    "TEND_CODEX_ROOT",
    "TEND_EFFORT",
    "TEND_HARNESS",
    "TEND_LIFECYCLE",
    "TEND_MODEL",
    "TEND_PROMPT",
    "TEND_PROXY_PORT",
    "TEND_RUNNER_HOME",
    "TEND_RUNNER_WORKSPACE",
    "TEND_RUN_DIR",
    "TEND_SANDBOX_SETUP",
    "TEND_SRT_ENTRY",
    "TEND_SRT_SECCOMP",
    "TEND_STEP_SUMMARY_DIR",
    "TEND_SYSTEM_PROMPT",
    "TEND_TIMEOUT_SEC",
}
MAX_FIXED_EXPORT = 64 * 1024 * 1024
MAX_FINAL_MESSAGE = 256 * 1024
MAX_STEP_SUMMARY = 512 * 1024


class Cancelled(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


@contextlib.contextmanager
def raise_on_cancel() -> Iterator[None]:
    """Turn runner cancellation into control flow that reaches the UID reap."""
    previous: dict[signal.Signals, signal.Handlers] = {}

    def cancel(signum: int, _frame: FrameType | None) -> None:
        raise Cancelled(signum)

    for watched in (signal.SIGINT, signal.SIGTERM):
        previous[watched] = signal.signal(watched, cancel)
    try:
        yield
    finally:
        for watched, handler in previous.items():
            signal.signal(watched, handler)


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is unset")
    return value


def reap(sandbox: str) -> bool:
    subprocess.run(
        ["/usr/bin/sudo", "/usr/bin/pkill", "-KILL", "-u", sandbox],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return (
        subprocess.run(
            ["/usr/bin/pgrep", "-u", sandbox],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 1
    )


def write_trusted(path: Path, body: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(descriptor, view) :]
    finally:
        os.close(descriptor)


def main() -> int:
    sandbox = required("SANDBOX")
    runner_temp = Path(required("RUNNER_TEMP")).resolve(strict=True)
    real_output = Path(required("GITHUB_OUTPUT"))
    export_dir = runner_temp / "tend-agent-export"
    export_dir.mkdir(mode=0o700)
    run_dir = Path(required("TEND_RUN_DIR"))
    step_summary_dir = Path(required("TEND_STEP_SUMMARY_DIR"))
    inner_output = run_dir / "tend-step-output"

    environment = _sandbox.launch_env(required("AGENT_ENV_FILE"))
    environment.extend(
        f"{name}={os.environ[name]}"
        for name in sorted(PASSTHROUGH)
        if os.environ.get(name)
    )
    environment.extend(
        [
            f"GITHUB_OUTPUT={inner_output}",
            f"RUNNER_TEMP={run_dir}",
        ]
    )
    argv = [
        "/usr/bin/sudo",
        "-u",
        sandbox,
        "/usr/bin/env",
        "-i",
        *environment,
        required("NODE_BIN"),
        str(Path(__file__).with_name("sandbox_runtime.mjs")),
    ]

    status = 1
    reaped = False
    try:
        with raise_on_cancel():
            status = subprocess.run(
                argv, stdin=subprocess.DEVNULL, check=False
            ).returncode
    except Cancelled as cancelled:
        status = 128 + cancelled.signum
    finally:
        reaped = reap(sandbox)

    with real_output.open("a", encoding="utf-8") as stream:
        stream.write(f"sandbox_reaped={'true' if reaped else 'false'}\n")
        if reaped:
            harness = os.environ.get("TEND_HARNESS")
            if harness == "claude":
                exported_stream = export_dir / "claude-stream.json"
                body = read_regular_nofollow(
                    run_dir / "tend-stream.json", max_bytes=MAX_FIXED_EXPORT
                )
                write_trusted(exported_stream, body or b"")
                stderr = read_regular_nofollow(
                    run_dir / "tend-claude-stderr.log", max_bytes=MAX_FIXED_EXPORT
                )
                write_trusted(runner_temp / "tend-claude-stderr.log", stderr or b"")
                stream.write(f"stream_json={exported_stream}\n")
            elif harness == "codex":
                message = read_regular_nofollow(
                    run_dir / "codex-final-message.md", max_bytes=MAX_FINAL_MESSAGE
                )
                if message is not None:
                    stream.write(
                        f"final_message={base64.b64encode(message).decode('ascii')}\n"
                    )
    if reaped:
        try:
            skill_summary = read_regular_nofollow(
                step_summary_dir / "step-summary.md", max_bytes=MAX_STEP_SUMMARY
            )
        except (OSError, ValueError) as problem:
            print(f"::warning::ignored invalid skill step summary: {problem}", flush=True)
        else:
            if skill_summary:
                with Path(required("GITHUB_STEP_SUMMARY")).open("ab") as summary:
                    summary.write(skill_summary)
                    if not skill_summary.endswith(b"\n"):
                        summary.write(b"\n")
                    summary.write(b"\n")
    if not reaped:
        print("::error::sandbox UID still owns a live process after reap", flush=True)
        return 1
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError) as problem:
        print(f"sandbox supervisor: {problem}", file=sys.stderr)
        raise SystemExit(1) from None
