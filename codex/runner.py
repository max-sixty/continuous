# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Run the three stateful phases of Tend's Codex harness.

The shared SRT supervisor owns the execution lifetime; Tend's proxies own
credentials. This module owns only Codex-specific mechanics that benefit from
argv construction and file handling: installing the runner plugin, staging the
global instructions, and writing the fixed final-message file around
``codex exec``.
"""

from __future__ import annotations

import json
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


def _sandbox_command(*args: str) -> list[str]:
    """Run a preparation command as the agent uid before SRT starts."""
    environment = _sandbox.agent_env(_required_path("AGENT_ENV_FILE"))
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
    if os.environ.get("TEND_INSIDE_SANDBOX") != "1":
        raise RuntimeError("Codex may run only inside the SRT lifecycle")
    codex = str(_required_path("CODEX_BIN"))
    auth_mode = os.environ.get("AUTH_MODE", "")
    auth_args: list[str]
    if auth_mode == "api-key":
        proxy_url = os.environ.get("CODEX_PROXY_URL", "")
        if not proxy_url:
            raise ValueError("CODEX_PROXY_URL is unset")
        tool_no_proxy = os.environ.get("NO_PROXY", "")
        tool_no_proxy_lower = os.environ.get("no_proxy", "")
        auth_args = [
            "--config",
            (
                "model_providers.tend-openai={ name = 'Tend OpenAI proxy', "
                f"base_url = '{proxy_url}/v1', wire_api = 'responses' }}"
            ),
            "--config",
            'model_provider="tend-openai"',
            # Only Codex's own model client must cross SRT's HTTP broker to
            # reach the runner-owned Responses proxy. Restore SRT's loopback
            # exclusions for shell tools so sandbox-local test servers remain
            # local to the sandbox.
            "--config",
            f"shell_environment_policy.set.NO_PROXY={json.dumps(tool_no_proxy)}",
            "--config",
            f"shell_environment_policy.set.no_proxy={json.dumps(tool_no_proxy_lower)}",
        ]
    elif auth_mode == "subscription":
        auth_args = []
    else:
        raise ValueError(f"unknown AUTH_MODE: {auth_mode or '<unset>'}")
    output_file = _required_path("TEND_RUN_DIR") / "codex-final-message.md"
    _run(["/usr/bin/rm", "-f", "--", str(output_file)])
    args = [
        codex,
        "exec",
        *(arg for arg in os.environ.get("EXTRA_ARGS", "").splitlines() if arg),
        "--model",
        os.environ.get("MODEL", ""),
        # SRT is the sole execution sandbox. A nested Codex sandbox creates a
        # second, divergent policy surface and is deliberately not selected.
        "--dangerously-bypass-approvals-and-sandbox",
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
    launch = ["/usr/bin/env"]
    if auth_mode == "api-key":
        # SRT owns the effective proxy variables. Its NO_PROXY includes
        # loopback, but the runner-owned Responses proxy lives on host
        # loopback, so only Codex's API client bypasses those exclusions.
        launch.extend(["NO_PROXY=", "no_proxy="])
    launch.extend(args)
    insert_at = launch.index(codex)
    launch[insert_at:insert_at] = [
        f"BOT_NAME={os.environ.get('BOT_NAME', '')}",
        f"BOT_ID={os.environ.get('BOT_ID', '')}",
        f"CI={os.environ.get('CI') or 'true'}",
    ]
    result = _run(launch, check=False)

    exists = _run(
        ["/usr/bin/test", "-f", str(output_file)],
        check=False,
    )
    encoded = None
    if exists.returncode == 0:
        encoded = _run(
            ["/usr/bin/base64", "-w0", str(output_file)],
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
    except (OSError, RuntimeError, ValueError) as error:
        print(f"codex runner: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode or 1) from None
