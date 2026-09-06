"""Verify the pinned Codex CLI contract used by Tend's composite action.

This credential-free CI probe covers the undocumented surfaces that can break
all Codex jobs during a version bump: access-only ChatGPT auth, ``codex exec``
flags and config, and plugin installation output and contents. Live model
behavior needs a separate authenticated integration test.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import codex_subscription_auth

REQUIRED_EXEC_FLAGS = (
    "--model",
    "--sandbox",
    "--output-last-message",
    "--config",
)
REQUIRED_EXEC_SWITCHES = ("--json", "--skip-git-repo-check")
UNKNOWN_CONFIG_ERROR = "unknown configuration field"


class SurfaceError(RuntimeError):
    """The installed Codex CLI no longer satisfies Tend's contract."""


def _run(
    codex: str,
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [codex, *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        command = " ".join(("codex", *args))
        raise SurfaceError(f"{command} exited {result.returncode}")
    return result


def _probe_consumer_auth(codex: str, codex_home: Path, env: dict[str, str]) -> None:
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"email": "probe@example.invalid"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    consumer = codex_subscription_auth.consumer_auth(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "access-only-probe",
                "refresh_token": "probe-refresh",
                "id_token": f"e30.{payload}.c2ln",
                "account_id": "probe-account",
            },
        }
    )
    auth_file = codex_home / "auth.json"
    codex_subscription_auth.prepare(
        codex_auth_json=json.dumps(consumer),
        openai_api_key="",
        destination=auth_file,
    )
    before = auth_file.read_bytes()
    output = _run(codex, "login", "status", env=env).stdout
    if "Logged in using ChatGPT" not in output:
        raise SurfaceError("codex login status did not load access-only ChatGPT auth")
    if auth_file.read_bytes() != before:
        raise SurfaceError("codex login status rewrote access-only ChatGPT auth")


def _probe_exec_flags(codex: str, env: dict[str, str]) -> None:
    help_text = _run(codex, "exec", "--help", env=env).stdout
    missing = [flag for flag in REQUIRED_EXEC_FLAGS if f"{flag} <" not in help_text]
    missing.extend(flag for flag in REQUIRED_EXEC_SWITCHES if flag not in help_text)
    if missing:
        raise SurfaceError(
            "codex exec is missing required options: " + ", ".join(missing)
        )


def _probe_plugin(codex: str, repository: Path, env: dict[str, str]) -> None:
    _run(codex, "plugin", "marketplace", "add", str(repository), env=env)
    output = _run(codex, "plugin", "add", "tend-ci-runner@tend", env=env).stdout
    print(output, end="" if output.endswith("\n") else "\n")
    match = re.search(r"^Installed plugin root: (.+)$", output, re.MULTILINE)
    if not match:
        raise SurfaceError(
            "codex plugin add no longer prints 'Installed plugin root: <path>'"
        )
    root = Path(match.group(1))
    required = (
        root / "skills" / "triage" / "SKILL.md",
        root / "scripts" / "list-recent-runs.sh",
    )
    if not root.is_dir() or not all(path.is_file() for path in required):
        raise SurfaceError("the installed tend-ci-runner plugin is incomplete")
    if not os.access(required[1], os.X_OK):
        raise SurfaceError("the installed list-recent-runs.sh is not executable")


def _probe_strict_config(codex: str, env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        unknown = _run(
            codex,
            "exec",
            "--strict-config",
            "-c",
            'model_reasoning_effortZZZ="low"',
            "x",
            cwd=cwd,
            env=env,
            check=False,
        ).stdout
        if UNKNOWN_CONFIG_ERROR not in unknown:
            raise SurfaceError(
                "codex --strict-config no longer rejects unknown -c keys"
            )
        known = _run(
            codex,
            "exec",
            "--strict-config",
            "-c",
            'model_reasoning_effort="low"',
            "x",
            cwd=cwd,
            env=env,
            check=False,
        ).stdout
        if UNKNOWN_CONFIG_ERROR in known:
            raise SurfaceError(
                "codex no longer accepts the model_reasoning_effort config key"
            )


def verify(repository: Path, codex: str = "codex") -> None:
    """Run every credential-free compatibility probe against ``codex``."""
    with tempfile.TemporaryDirectory() as directory:
        codex_home = Path(directory)
        env = os.environ.copy()
        env["CODEX_HOME"] = directory
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
        _probe_consumer_auth(codex, codex_home, env)
        _probe_exec_flags(codex, env)
        _probe_plugin(codex, repository, env)
        _probe_strict_config(codex, env)


def main() -> int:
    try:
        verify(Path.cwd())
    except SurfaceError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
