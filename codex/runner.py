# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Run the three stateful phases of Tend's Codex harness.

The composite action remains the lifecycle and secret boundary. This module
owns the Codex-specific mechanics that benefit from argv construction, output
parsing, and file handling: installing the runner plugin, staging the global
instructions, and preserving the final message around ``codex exec``.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT_PREFIX = "Installed plugin root: "


def _run(
    args: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE if capture else None,
        text=True,
        check=check,
        env=env,
    )


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is unset")
    return Path(value)


def _append_environment(name: str, value: str) -> None:
    with _required_path("GITHUB_ENV").open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def install_plugin() -> int:
    """Install tend-ci-runner and export its root for skill scripts."""
    marketplace_root = _required_path("ACTION_PATH").resolve().parent
    _run(["codex", "plugin", "marketplace", "add", str(marketplace_root)])
    installed = _run(["codex", "plugin", "add", "tend-ci-runner@tend"], capture=True)
    stdout = installed.stdout or ""
    sys.stdout.write(stdout)
    roots = [
        Path(line.removeprefix(PLUGIN_ROOT_PREFIX))
        for line in stdout.splitlines()
        if line.startswith(PLUGIN_ROOT_PREFIX)
    ]
    if len(roots) != 1 or not roots[0].is_dir():
        print(
            "::error::Failed to parse one existing "
            "'Installed plugin root: <path>' from codex plugin add output"
        )
        return 1
    _append_environment("CLAUDE_PLUGIN_ROOT", str(roots[0]))
    _run(["codex", "plugin", "list"], check=False)
    return 0


def _substitute_bot_name(text: str, bot_name: str) -> str:
    return text.replace("${BOT_NAME}", bot_name).replace("$BOT_NAME", bot_name)


def stage_agents() -> int:
    """Compose the harness-neutral prompt and Codex tail into AGENTS.md."""
    action_path = _required_path("ACTION_PATH").resolve()
    bot_name = os.environ.get("BOT_NAME", "")
    if not bot_name:
        raise ValueError("BOT_NAME is unset")
    shared = (action_path.parent / "shared/system-prompt.md").read_text()
    tail = (action_path / "agents-tail.md").read_text()
    body = (
        "# Tend CI guidance (Codex harness)\n\n"
        + _substitute_bot_name(shared, bot_name).rstrip("\n")
        + "\n\n"
        + _substitute_bot_name(tail, bot_name).rstrip("\n")
        + "\n"
    )
    agents = _required_path("HOME") / ".codex/AGENTS.md"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text(body)
    print(f"Staged AGENTS.md at {agents} ({len(body.splitlines())} lines)")
    return 0


def run_codex() -> int:
    """Run Codex and export its final message even when the process fails."""
    runner_temp = _required_path("RUNNER_TEMP")
    output_file = runner_temp / "codex-final-message.md"
    output_file.unlink(missing_ok=True)
    args = [
        "codex",
        "exec",
        "--model",
        os.environ.get("MODEL", ""),
        "--sandbox",
        os.environ.get("SANDBOX", ""),
        "--output-last-message",
        str(output_file),
    ]
    effort = os.environ.get("EFFORT", "")
    if effort:
        args.extend(["--config", f'model_reasoning_effort="{effort}"'])
    args.append(os.environ.get("PROMPT", ""))
    env = os.environ.copy()
    env["PATH"] = env.get("PATH", "") + os.pathsep + str(runner_temp / "tend-agent-uv")
    result = _run(args, check=False, env=env)

    if output_file.is_file():
        encoded = base64.b64encode(output_file.read_bytes()).decode("ascii")
        with _required_path("GITHUB_OUTPUT").open("a", encoding="utf-8") as stream:
            stream.write(f"final_message={encoded}\n")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["install-plugin"]:
        return install_plugin()
    if args == ["stage-agents"]:
        return stage_agents()
    if args == ["run"]:
        return run_codex()
    print(f"usage: {sys.argv[0]} install-plugin|stage-agents|run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"codex runner: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode or 1) from None
