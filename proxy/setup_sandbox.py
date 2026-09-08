# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Prepare the non-sudo agent user and its credential-injecting proxy.

This program runs as the privileged Actions runner after the independent agent
clone has been prepared. It exports the sandbox paths through ``GITHUB_ENV``,
hands only that disposable clone to ``tend-sandbox``, and starts mitmproxy with
the real GitHub credential and, for Claude, the real model credential. The
runner checkout remains runner-owned and the agent receives only dummy
credentials.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
SANDBOX = "tend-sandbox"
AGENT_HOME = Path(f"/home/{SANDBOX}")
PROXY_PORT = 8899
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
PROXY_CA_CERT = Path("/usr/local/share/ca-certificates/tend-proxy.crt")
TEND_RUN_DIR = AGENT_HOME / "run"
TEND_STEP_SUMMARY_DIR = Path("/tmp/claude")
TEND_AGENT_UV_DIR = AGENT_HOME / ".tend-uv/bin"
ALLOW_HOSTS = (
    r"^((api\.|codeload\.|uploads\.)?github\.com|raw\.githubusercontent\.com|"
    r"api\.anthropic\.com)(:[0-9]+)?$"
)
GITHUB_DUMMY = "ghp_tendproxydummy000000000000000000000"
OAUTH_DUMMY = "sk-ant-oat01-tendproxydummy0000000000000000000000000000"
API_KEY_DUMMY = "sk-ant-api03-tendproxydummy0000000000000000000000000000"
RESERVED_SANDBOX_ENV = {
    "HOME",
    "PATH",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "https_proxy",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_WORKSPACE",
    "CLAUDE_CODE_REMOTE",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_AUTH_JSON",
    "CODEX_HOME",
}
BLOCKED_COMMAND = """#!/bin/sh
printf "tend: %s came from the runner home and is unavailable; install it into ~/.local/bin with sandbox_setup, or point sandbox_path at a copy outside the runner home\n" "${0##*/}" >&2
exit 127
"""


def log(message: str) -> None:
    print(f"[setup-sandbox] {message}", flush=True)


def error(message: str) -> int:
    print(f"::error::{message}", flush=True)
    return 1


def command(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    input: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one trusted system command from an explicit argv."""
    return subprocess.run(
        argv,
        input=input,
        text=True,
        capture_output=capture,
        env=env,
        check=check,
    )


def sudo(
    *args: str,
    user: str | None = None,
    check: bool = True,
    capture: bool = False,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = ["/usr/bin/sudo"]
    if user:
        argv.extend(["-u", user])
    argv.extend(args)
    return command(argv, check=check, capture=capture, input=input)


def resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


@dataclass(frozen=True)
class Paths:
    workspace: Path
    runner_workspace: Path
    runner_temp: Path
    runtime_root: Path
    action_path: Path
    tend_uv_dir: Path
    github_env: Path
    runner_home: Path

    @property
    def agent_env_file(self) -> Path:
        return self.runtime_root / "agent-env"

    @property
    def confdir(self) -> Path:
        return self.runner_temp / "tend-proxy"

    @property
    def proxy_log(self) -> Path:
        return self.runner_temp / "tend-proxy.log"

    @property
    def proxy_pid(self) -> Path:
        return self.runner_temp / "tend-proxy.pid"


@dataclass(frozen=True)
class PathPlan:
    agent_path: list[str]
    dropped_home_paths: list[str]
    blocked_commands: list[str]
    blocked_path: Path | None


def configured_paths(raw: str, *, paths: Paths) -> list[str]:
    """Expand and validate adopter-provided sandbox PATH prefixes."""
    entries: list[str] = []
    for entry in raw.split("\n"):
        if not entry:
            continue
        if entry == "~":
            entry = str(AGENT_HOME)
        elif entry.startswith("~/"):
            entry = str(AGENT_HOME / entry[2:])
        canonical = resolved(entry)
        if within(canonical, paths.runner_home) and not within(
            canonical, paths.workspace
        ):
            raise ValueError(
                f"sandbox_path entry '{entry}' is under the runner's home outside "
                "the checkout. Install the tool into the sandbox with "
                "sandbox_setup: instead."
            )
        append_unique(entries, entry)
    return entries


def sandbox_can_execute(path: Path) -> bool:
    return (
        sudo(
            "/usr/bin/test",
            "-x",
            str(path),
            user=SANDBOX,
            check=False,
        ).returncode
        == 0
    )


def plan_agent_path(
    *,
    runner_tool_path: str,
    extras: list[str],
    paths: Paths,
    can_execute: Callable[[Path], bool] = sandbox_can_execute,
) -> PathPlan:
    """Translate the runner PATH across the UID and home-directory boundary."""
    agent_path = list(extras)
    append_unique(agent_path, str(AGENT_HOME / ".local/bin"))
    prefix_count = len(agent_path)
    dropped: list[str] = []
    blocked: list[str] = []

    for entry in runner_tool_path.split(os.pathsep):
        if not entry:
            continue
        source = Path(entry)
        try:
            canonical = source.resolve(strict=True)
        except OSError:
            continue

        target = canonical
        shared_workspace = within(canonical, paths.workspace)
        drop = False
        if shared_workspace:
            pass
        elif canonical == paths.runner_home:
            drop = True
        elif within(canonical, paths.runner_home):
            target = AGENT_HOME / canonical.relative_to(paths.runner_home)
            if not target.is_dir() or not can_execute(target):
                drop = True
        if drop:
            append_unique(dropped, str(canonical))
            if canonical.is_dir() and os.access(canonical, os.R_OK):
                for candidate in canonical.iterdir():
                    if not candidate.is_file() or not os.access(candidate, os.X_OK):
                        continue
                    if candidate.name in {"uv", "uvx"}:
                        continue
                    selected = shutil.which(candidate.name, path=runner_tool_path)
                    if selected and resolved(Path(selected).parent) == canonical:
                        append_unique(blocked, candidate.name)
            continue
        if shared_workspace or (target.is_dir() and can_execute(target)):
            append_unique(agent_path, str(target))

    for base in ("/usr/local/bin", "/usr/bin", "/bin"):
        append_unique(agent_path, base)

    blocked_path = AGENT_HOME / ".tend-blocked/bin" if blocked else None
    if blocked_path:
        agent_path.insert(prefix_count, str(blocked_path))
    append_unique(agent_path, str(TEND_AGENT_UV_DIR))
    return PathPlan(agent_path, dropped, blocked, blocked_path)


def install_blocked_commands(plan: PathPlan) -> None:
    if not plan.blocked_path:
        return
    root = plan.blocked_path.parent
    sudo("/usr/bin/mkdir", "-p", str(plan.blocked_path), user=SANDBOX)
    sudo(
        "/usr/bin/tee",
        str(root / "unavailable"),
        user=SANDBOX,
        input=BLOCKED_COMMAND,
        capture=True,
    )
    sudo("/usr/bin/chmod", "+x", str(root / "unavailable"), user=SANDBOX)
    for name in plan.blocked_commands:
        sudo(
            "/usr/bin/ln",
            "-sfn",
            "../unavailable",
            str(plan.blocked_path / name),
            user=SANDBOX,
        )


def base_agent_env(
    agent_path: str,
    anthropic_dummy: tuple[str, str] | None,
    *,
    workspace: Path,
) -> list[str]:
    """Return the newline-delimited assignments passed across the UID boundary."""
    values = {
        "HOME": str(AGENT_HOME),
        "PATH": agent_path,
        "XDG_CONFIG_HOME": str(AGENT_HOME / ".config"),
        "XDG_CACHE_HOME": str(AGENT_HOME / ".cache"),
        "XDG_DATA_HOME": str(AGENT_HOME / ".local/share"),
        "XDG_STATE_HOME": str(AGENT_HOME / ".local/state"),
        "HTTPS_PROXY": PROXY_URL,
        "HTTP_PROXY": PROXY_URL,
        "https_proxy": PROXY_URL,
        "http_proxy": PROXY_URL,
        "NODE_EXTRA_CA_CERTS": str(PROXY_CA_CERT),
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "GH_TOKEN": GITHUB_DUMMY,
        "GITHUB_TOKEN": GITHUB_DUMMY,
        "GITHUB_WORKSPACE": str(workspace),
        "CLAUDE_CODE_REMOTE": "1",
    }
    if anthropic_dummy:
        values[anthropic_dummy[0]] = anthropic_dummy[1]
    if not values.keys() <= RESERVED_SANDBOX_ENV:
        raise AssertionError("agent environment contains an unreserved key")
    return [f"{name}={value}" for name, value in values.items()]


def adopter_env(raw: str) -> list[str]:
    """Validate the hand-edited workflow boundary for ``sandbox_env``."""
    assignments: list[str] = []
    for line in raw.split("\n"):
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"sandbox_env line is not NAME=VALUE: '{line}'")
        name = line.split("=", 1)[0]
        if name in RESERVED_SANDBOX_ENV:
            raise ValueError(f"sandbox_env may not set reserved key '{name}'")
        assignments.append(line)
    return assignments


def write_agent_environment(
    *, paths: Paths, plan: PathPlan, anthropic_dummy: tuple[str, str] | None
) -> str:
    agent_path = os.pathsep.join(plan.agent_path)
    assignments = base_agent_env(agent_path, anthropic_dummy, workspace=paths.workspace)
    assignments.extend(adopter_env(os.environ.get("TEND_SANDBOX_ENV", "")))
    paths.agent_env_file.write_text("\n".join(assignments) + "\n", encoding="utf-8")
    exports = {
        "SANDBOX": SANDBOX,
        "AGENT_HOME": str(AGENT_HOME),
        "TEND_AGENT_UV_DIR": str(TEND_AGENT_UV_DIR),
        "PROXY_URL": PROXY_URL,
        "TEND_PROXY_PORT": str(PROXY_PORT),
        "TEND_RUN_DIR": str(TEND_RUN_DIR),
        "TEND_STEP_SUMMARY_DIR": str(TEND_STEP_SUMMARY_DIR),
        "PROXY_CA_CERT": str(PROXY_CA_CERT),
        "AGENT_ENV_FILE": str(paths.agent_env_file),
        "AGENT_PATH": agent_path,
        "TEND_BLOCKED_PATH": str(plan.blocked_path or ""),
        "TEND_RUNNER_HOME": str(paths.runner_home),
    }
    with paths.github_env.open("a", encoding="utf-8") as stream:
        for name, value in exports.items():
            stream.write(f"{name}={value}\n")
    return agent_path


def ensure_sandbox_user() -> None:
    exists = (
        command(["/usr/bin/id", SANDBOX], check=False, capture=True).returncode == 0
    )
    if not exists:
        sudo(
            "/usr/sbin/useradd",
            "-m",
            "-s",
            "/usr/bin/bash",
            SANDBOX,
        )
    uid = command(["/usr/bin/id", "-u", SANDBOX], capture=True).stdout.strip()
    log(f"user {SANDBOX} uid={uid}")


def configure_global_gitignore() -> None:
    git_config = AGENT_HOME / ".config/git"
    ignore = git_config / "ignore"
    sudo("/usr/bin/mkdir", "-p", str(git_config), user=SANDBOX)
    sudo(
        "/usr/bin/tee",
        str(ignore),
        user=SANDBOX,
        input="/.claude/settings.local.json\n",
        capture=True,
    )
    sudo(
        "/usr/bin/env",
        f"HOME={AGENT_HOME}",
        f"XDG_CONFIG_HOME={AGENT_HOME / '.config'}",
        "/usr/bin/git",
        "-C",
        str(AGENT_HOME),
        "config",
        "--global",
        "core.excludesFile",
        str(ignore),
        user=SANDBOX,
    )
    log(f"global gitignore at {ignore}")


def strip_checkout_credentials(paths: Paths) -> bool:
    """Remove every persisted checkout credential and verify none resolves."""
    git = ["/usr/bin/git", "-C", str(paths.workspace), "config", "--local"]
    command(
        [*git, "--unset-all", "http.https://github.com/.extraheader"],
        check=False,
        capture=True,
    )
    includes = command(
        [*git, "--name-only", "--get-regexp", r"^includeif\."],
        check=False,
        capture=True,
    ).stdout.splitlines()
    for key in includes:
        if key:
            command([*git, "--unset", key], check=False, capture=True)
    sudo(
        "/usr/bin/find",
        str(paths.runner_temp),
        "-maxdepth",
        "2",
        "-name",
        "git-credentials-*",
        "-delete",
        check=False,
        capture=True,
    )

    listing = command([*git, "--list"], check=False, capture=True).stdout
    residual = [
        line
        for line in listing.splitlines()
        if re.search(r"extraheader=|^includeif\.gitdir", line, re.IGNORECASE)
    ]
    if residual:
        error(
            "failed to neutralize the persisted git credential in "
            f"{paths.workspace}/.git/config"
        )
        print(*residual, sep="\n")
        return False
    log("neutralized persisted git credentials")
    return True


def handoff_workspace(paths: Paths) -> bool:
    """Give the sandbox UID only its disposable checkout."""
    if paths.workspace == paths.runner_workspace or within(
        paths.workspace, paths.runner_workspace
    ):
        error("agent workspace must be independent of the runner checkout")
        return False
    sudo(
        "/usr/bin/chown",
        "--recursive",
        "--no-dereference",
        f"{SANDBOX}:{SANDBOX}",
        str(paths.workspace),
    )
    readable = (
        sudo(
            "/usr/bin/test",
            "-r",
            str(paths.workspace / ".git/config"),
            user=SANDBOX,
            check=False,
        ).returncode
        == 0
    )
    if not readable:
        error(f"sandbox cannot access the workspace at {paths.workspace}")
        return False
    log(f"workspace handed to {SANDBOX}")
    sudo("/usr/bin/mkdir", "-p", str(TEND_RUN_DIR), user=SANDBOX)
    sudo("/usr/bin/mkdir", "-p", str(TEND_STEP_SUMMARY_DIR), user=SANDBOX)
    log(f"run dir {TEND_RUN_DIR}")
    return True


def _show_proxy_log(path: Path) -> None:
    try:
        sys.stdout.write(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass


def uvx_command(paths: Paths, *, version: str, args: list[str]) -> list[str]:
    """Build a uv tool command pinned to the runner's trusted Python."""
    return [
        str(paths.tend_uv_dir / "uvx"),
        "--no-config",
        "--no-python-downloads",
        "--python",
        "/usr/bin/python3",
        "--from",
        f"mitmproxy=={version}",
        "mitmdump",
        *args,
    ]


def start_proxy(paths: Paths, *, version: str) -> bool:
    paths.confdir.mkdir(parents=True, exist_ok=True)
    paths.confdir.chmod(0o700)
    command(uvx_command(paths, version=version, args=["--version"]))
    log("starting proxy")
    proxy_log = paths.proxy_log.open("wb")
    process = subprocess.Popen(
        uvx_command(
            paths,
            version=version,
            args=[
                "-s",
                str(paths.action_path / "proxy/inject_credentials.py"),
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(PROXY_PORT),
                "--set",
                f"confdir={paths.confdir}",
                "--allow-hosts",
                ALLOW_HOSTS,
            ],
        ),
        stdin=subprocess.DEVNULL,
        stdout=proxy_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=os.environ.copy(),
    )
    proxy_log.close()
    paths.proxy_pid.write_text(f"{process.pid}\n", encoding="utf-8")

    ready = False
    for _ in range(60):
        if process.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=0.2):
                ready = True
                break
        except OSError:
            time.sleep(0.5)
    if not ready:
        error(f"mitmdump never accepted a connection on {PROXY_PORT}")
        _show_proxy_log(paths.proxy_log)
        return False

    generated_ca = paths.confdir / "mitmproxy-ca-cert.pem"
    if not generated_ca.is_file():
        error("proxy CA not generated; mitmdump failed to start")
        _show_proxy_log(paths.proxy_log)
        return False
    sudo("/usr/bin/cp", str(generated_ca), str(PROXY_CA_CERT))
    sudo("/usr/sbin/update-ca-certificates", capture=True)
    log(f"proxy up at {PROXY_URL}; CA trusted")
    return True


def required_path(name: str) -> Path:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is unset")
    return resolved(value)


def runner_home() -> Path:
    """Return the runner account's home without trusting the job environment."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def main() -> int:
    runner_tool_path = os.environ.pop(
        "TEND_RUNNER_TOOL_PATH", os.environ.get("PATH", "")
    )
    os.environ["PATH"] = SYSTEM_PATH
    if not os.environ.get("TEND_GH_TOKEN"):
        return error("TEND_GH_TOKEN is unset; cannot start the credential proxy")
    version = os.environ.get("MITMPROXY_VERSION", "")
    if not version:
        return error("MITMPROXY_VERSION is unset; the action must pin it")
    workspace_value = os.environ.get("TEND_AGENT_WORKSPACE", "")
    if not workspace_value or not Path(workspace_value).is_dir():
        return error("TEND_AGENT_WORKSPACE must name the prepared disposable checkout")
    workspace = resolved(workspace_value)
    if workspace == Path("/"):
        return error("TEND_AGENT_WORKSPACE may not be the filesystem root")
    runner_workspace_value = os.environ.get("GITHUB_WORKSPACE", "")
    if not runner_workspace_value or not Path(runner_workspace_value).is_dir():
        return error("GITHUB_WORKSPACE must name the runner checkout")
    runner_workspace = resolved(runner_workspace_value)

    try:
        paths = Paths(
            workspace=workspace,
            runner_workspace=runner_workspace,
            runner_temp=required_path("RUNNER_TEMP"),
            runtime_root=required_path("TEND_RUNTIME_ROOT"),
            action_path=required_path("ACTION_PATH"),
            tend_uv_dir=required_path("TEND_UV_DIR"),
            github_env=required_path("GITHUB_ENV"),
            runner_home=runner_home(),
        )
    except ValueError as problem:
        return error(str(problem))

    ensure_sandbox_user()
    configure_global_gitignore()
    github_only = os.environ.get("TEND_GITHUB_ONLY") == "1"
    if github_only:
        os.environ.pop("TEND_ANTHROPIC_OAUTH_TOKEN", None)
        os.environ.pop("TEND_ANTHROPIC_API_KEY", None)
        anthropic_dummy = None
    elif os.environ.get("TEND_ANTHROPIC_OAUTH_TOKEN"):
        os.environ.pop("TEND_ANTHROPIC_API_KEY", None)
        anthropic_dummy = ("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_DUMMY)
    else:
        anthropic_dummy = ("ANTHROPIC_API_KEY", API_KEY_DUMMY)

    try:
        extras = configured_paths(os.environ.get("TEND_SANDBOX_PATH", ""), paths=paths)
        plan = plan_agent_path(
            runner_tool_path=runner_tool_path,
            extras=extras,
            paths=paths,
        )
        install_blocked_commands(plan)
        if plan.dropped_home_paths:
            log(
                "runner-home PATH entries unavailable in sandbox: "
                + " ".join(plan.dropped_home_paths)
            )
            log("install any required home-scoped tools with sandbox_setup:")
        if plan.blocked_commands:
            log(
                "runner-home commands blocked from shared fallbacks: "
                + " ".join(plan.blocked_commands)
            )
        agent_path = write_agent_environment(
            paths=paths, plan=plan, anthropic_dummy=anthropic_dummy
        )
    except ValueError as problem:
        return error(str(problem))
    log(f"sandbox PATH: {agent_path}")

    if not strip_checkout_credentials(paths):
        return 1
    if not handoff_workspace(paths):
        return 1
    if not start_proxy(paths, version=version):
        return 1
    auth = "GitHub" if github_only else "GitHub + Anthropic"
    log(f"done; agent runs as {SANDBOX}, {auth} auth via the proxy")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as problem:
        if problem.stderr:
            sys.stderr.write(problem.stderr)
        raise SystemExit(problem.returncode or 1) from None
