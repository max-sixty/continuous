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

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared/steps"))

import _sandbox

PLUGIN_ROOT_PREFIX = "Installed plugin root: "


def _run(
    args: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input,
        stdout=subprocess.PIPE if capture else None,
        text=True,
        check=check,
    )


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is unset")
    return Path(value)


def _append_agent_environment(name: str, value: str) -> None:
    with _required_path("AGENT_ENV_FILE").open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def _sandbox_command(*args: str, github_context: bool = False) -> list[str]:
    environment = (
        _sandbox.launch_env(_required_path("AGENT_ENV_FILE"))
        if github_context
        else _sandbox.agent_env(_required_path("AGENT_ENV_FILE"))
    )
    return [
        "/usr/bin/sudo",
        "-u",
        os.environ.get("SANDBOX", ""),
        "/usr/bin/env",
        *environment,
        *args,
    ]


def install_plugin() -> int:
    """Install tend-ci-runner and export its root for skill scripts."""
    sandbox = os.environ.get("SANDBOX", "")
    if not sandbox:
        raise ValueError("SANDBOX is unset")
    action_path = _required_path("ACTION_PATH").resolve()
    marketplace_source = action_path.parent
    agent_home = _required_path("AGENT_HOME").resolve()
    marketplace_root = agent_home / "tend-marketplace"
    _run(["/usr/bin/sudo", "/usr/bin/rm", "-rf", "--", str(marketplace_root)])
    _run(["/usr/bin/sudo", "/usr/bin/mkdir", "-p", str(marketplace_root)])
    _run(
        [
            "/usr/bin/sudo",
            "/usr/bin/cp",
            "-a",
            str(marketplace_source / ".agents"),
            str(marketplace_source / "plugins"),
            f"{marketplace_root}/",
        ]
    )
    _run(
        [
            "/usr/bin/sudo",
            "/usr/bin/chown",
            "-R",
            f"{sandbox}:{sandbox}",
            str(marketplace_root),
        ]
    )
    codex = str(_required_path("CODEX_BIN"))
    _run(_sandbox_command(codex, "plugin", "marketplace", "add", str(marketplace_root)))
    installed = _run(
        _sandbox_command(codex, "plugin", "add", "tend-ci-runner@tend"),
        capture=True,
    )
    stdout = installed.stdout or ""
    sys.stdout.write(stdout)
    roots = [
        Path(line.removeprefix(PLUGIN_ROOT_PREFIX)).resolve()
        for line in stdout.splitlines()
        if line.startswith(PLUGIN_ROOT_PREFIX)
    ]
    valid = (
        len(roots) == 1
        and roots[0].is_relative_to(agent_home)
        and _run(
            [
                "/usr/bin/sudo",
                "-u",
                sandbox,
                "/usr/bin/test",
                "-d",
                str(roots[0]),
            ],
            check=False,
        ).returncode
        == 0
    )
    if not valid:
        print(
            "::error::Failed to parse one sandbox-owned "
            "'Installed plugin root: <path>' from codex plugin add output"
        )
        return 1
    _append_agent_environment("CLAUDE_PLUGIN_ROOT", str(roots[0]))
    _run(_sandbox_command(codex, "plugin", "list"), check=False)
    return 0


def _substitute_runtime(text: str, bot_name: str, merge: str) -> str:
    return (
        text.replace("${BOT_NAME}", bot_name)
        .replace("$BOT_NAME", bot_name)
        .replace("${TEND_MERGE}", merge)
        .replace("$TEND_MERGE", merge)
    )


def stage_agents() -> int:
    """Compose the harness-neutral prompt and Codex tail into AGENTS.md."""
    action_path = _required_path("ACTION_PATH").resolve()
    bot_name = os.environ.get("BOT_NAME", "")
    if not bot_name:
        raise ValueError("BOT_NAME is unset")
    merge = os.environ.get("TEND_MERGE", "")
    if merge not in {"maintainer", "yolo"}:
        raise ValueError(f"unknown TEND_MERGE: {merge or '<unset>'}")
    shared = (action_path.parent / "shared/system-prompt.md").read_text()
    tail = (action_path / "agents-tail.md").read_text()
    body = (
        "# Tend CI guidance (Codex harness)\n\n"
        + _substitute_runtime(shared, bot_name, merge).rstrip("\n")
        + "\n\n"
        + _substitute_runtime(tail, bot_name, merge).rstrip("\n")
        + "\n"
    )
    sandbox = os.environ.get("SANDBOX", "")
    if not sandbox:
        raise ValueError("SANDBOX is unset")
    agents = _required_path("AGENT_HOME") / ".codex/AGENTS.md"
    _run(["/usr/bin/sudo", "-u", sandbox, "/usr/bin/mkdir", "-p", str(agents.parent)])
    _run(
        ["/usr/bin/sudo", "-u", sandbox, "/usr/bin/tee", str(agents)],
        capture=True,
        input=body,
    )
    print(f"Staged AGENTS.md at {agents} ({len(body.splitlines())} lines)")
    return 0


def run_codex() -> int:
    """Run Codex and export its final message even when the process fails."""
    sandbox = os.environ.get("SANDBOX", "")
    if not sandbox:
        raise ValueError("SANDBOX is unset")
    codex = str(_required_path("CODEX_BIN"))
    auth_mode = os.environ.get("AUTH_MODE", "")
    auth_args: list[str]
    if auth_mode == "api-key":
        proxy_url = os.environ.get("CODEX_PROXY_URL", "")
        if not proxy_url:
            raise ValueError("CODEX_PROXY_URL is unset")
        auth_args = [
            "--config",
            (
                "model_providers.tend-openai={ name = 'Tend OpenAI proxy', "
                f"base_url = '{proxy_url}/v1', wire_api = 'responses' }}"
            ),
            "--config",
            'model_provider="tend-openai"',
        ]
    elif auth_mode == "subscription":
        auth_args = []
    else:
        raise ValueError(f"unknown AUTH_MODE: {auth_mode or '<unset>'}")
    output_file = _required_path("TEND_RUN_DIR") / "codex-final-message.md"
    _run(["/usr/bin/sudo", "-u", sandbox, "/usr/bin/rm", "-f", "--", str(output_file)])
    args = [
        codex,
        "exec",
        *(arg for arg in os.environ.get("EXTRA_ARGS", "").splitlines() if arg),
        "--model",
        os.environ.get("MODEL", ""),
        "--sandbox",
        os.environ.get("CODEX_SANDBOX_MODE", ""),
        "--output-last-message",
        str(output_file),
        *auth_args,
        "--config",
        'cli_auth_credentials_store="file"',
    ]
    effort = os.environ.get("EFFORT", "")
    if effort:
        args.extend(["--config", f'model_reasoning_effort="{effort}"'])
    args.append(os.environ.get("PROMPT", ""))
    launch = _sandbox_command(
        *args,
        github_context=True,
    )
    insert_at = launch.index(codex)
    launch[insert_at:insert_at] = [
        f"BOT_NAME={os.environ.get('BOT_NAME', '')}",
        f"BOT_ID={os.environ.get('BOT_ID', '')}",
        f"CI={os.environ.get('CI') or 'true'}",
    ]
    result = _run(launch, check=False)

    exists = _run(
        [
            "/usr/bin/sudo",
            "-u",
            sandbox,
            "/usr/bin/test",
            "-f",
            str(output_file),
        ],
        check=False,
    )
    encoded = None
    if exists.returncode == 0:
        encoded = _run(
            [
                "/usr/bin/sudo",
                "-u",
                sandbox,
                "/usr/bin/base64",
                "-w0",
                str(output_file),
            ],
            capture=True,
            check=False,
        )
    if encoded and encoded.returncode == 0:
        with _required_path("GITHUB_OUTPUT").open("a", encoding="utf-8") as stream:
            stream.write(f"final_message={encoded.stdout or ''}\n")
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
