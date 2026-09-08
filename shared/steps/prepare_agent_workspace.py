# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Create the only repository tree the agent is allowed to access.

The Actions checkout is trusted orchestration state.  This step clones the
same repository into ``RUNNER_TEMP`` without local object sharing, selects the
event topology with runner/system Git configuration disabled, removes every
temporary credential, and exports the resulting path as
``TEND_AGENT_WORKSPACE``.  The runner checkout is never handed to the agent and
never changes ownership.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

STEP = "prepare-agent-workspace"
ASKPASS = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' x-access-token ;;
  *Password*) printf '%s\\n' "$TEND_CLONE_TOKEN" ;;
  *) exit 1 ;;
esac
"""
OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


def log(message: str) -> None:
    print(f"[{STEP}] {message}", flush=True)


def fail(message: str) -> int:
    print(f"::error::{message}", flush=True)
    return 1


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def isolated_git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment that cannot inherit runner Git execution hooks."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if extra:
        environment.update(extra)
    return environment


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is unset")
    return value


def api_json(path: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise TypeError(f"GitHub API returned a non-object for {path}")
    return value


def event_payload() -> dict[str, Any]:
    with Path(required("GITHUB_EVENT_PATH")).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError("GITHUB_EVENT_PATH must contain a JSON object")
    return value


def event_number(payload: dict[str, Any]) -> int:
    raw = payload.get("number")
    if raw is None and isinstance(payload.get("issue"), dict):
        raw = payload["issue"].get("number")
    if raw is None and isinstance(payload.get("client_payload"), dict):
        raw = payload["client_payload"].get("pr")
    if isinstance(raw, str) and raw.isdecimal():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError("event does not identify a positive PR or issue number")
    return raw


def issue_comment_targets_pull_request(payload: dict[str, Any]) -> bool:
    issue = payload.get("issue")
    return isinstance(issue, dict) and isinstance(issue.get("pull_request"), dict)


def git(
    *args: str,
    cwd: Path,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
):
    return run(
        ["/usr/bin/git", *args],
        cwd=cwd,
        check=check,
        capture=capture,
        env=isolated_git_environment(env),
    )


@contextmanager
def git_auth(token: str, directory: Path) -> Iterator[dict[str, str]]:
    askpass = directory / f"tend-git-askpass-{os.getpid()}"
    askpass.write_text(ASKPASS, encoding="utf-8")
    askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    env = isolated_git_environment(
        {
            "GIT_ASKPASS": str(askpass),
            "TEND_CLONE_TOKEN": token,
        }
    )
    try:
        yield env
    finally:
        askpass.unlink(missing_ok=True)


def checkout_base(workspace: Path, branch: str, sha: str) -> str:
    git("checkout", "-B", branch, sha, cwd=workspace)
    git("branch", "--set-upstream-to", f"origin/{branch}", branch, cwd=workspace)
    return f"base branch {branch} at {sha}"


def checkout_review(workspace: Path, number: int, token: str = "") -> str:
    environment = git_auth(token, workspace.parent) if token else null_auth()
    with environment as auth_env:
        for kind in ("merge", "head"):
            source = f"refs/pull/{number}/{kind}"
            result = git(
                "fetch",
                "--no-tags",
                "origin",
                source,
                cwd=workspace,
                check=False,
                env=auth_env,
            )
            if result.returncode == 0:
                git("checkout", "--detach", "FETCH_HEAD", cwd=workspace)
                return f"PR #{number} {kind} ref"
    raise ValueError(f"neither merge nor head ref exists for PR #{number}")


@contextmanager
def null_auth() -> Iterator[None]:
    yield None


def checkout_mention(
    workspace: Path,
    *,
    repository: str,
    number: int,
    token: str,
    base_branch: str,
    base_sha: str,
) -> tuple[str, str]:
    pr = api_json(f"/repos/{repository}/pulls/{number}", token)
    if pr.get("state") != "open":
        return checkout_base(workspace, base_branch, base_sha), pull_base_sha(
            pr, number
        )
    head = pr.get("head")
    if not isinstance(head, dict) or not isinstance(head.get("repo"), dict):
        raise TypeError(f"PR #{number} has no fetchable head repository")
    branch = head.get("ref")
    head_sha = head.get("sha")
    clone_url = head["repo"].get("clone_url")
    if not isinstance(branch, str) or not isinstance(clone_url, str):
        raise TypeError(f"PR #{number} has an invalid head topology")
    head_sha = validated_object_id(head_sha, f"PR #{number} head")
    git("check-ref-format", "--branch", branch, cwd=workspace)
    git("remote", "add", "tend-head", clone_url, cwd=workspace)
    with git_auth(token, workspace.parent) as auth_env:
        git(
            "fetch",
            "--no-tags",
            "tend-head",
            f"{head_sha}:refs/remotes/tend-head/{branch}",
            cwd=workspace,
            env=auth_env,
        )
    git("checkout", "-B", branch, f"refs/remotes/tend-head/{branch}", cwd=workspace)
    git("branch", "--set-upstream-to", f"tend-head/{branch}", branch, cwd=workspace)
    return (
        f"open PR #{number} head {head['repo'].get('full_name')}:{branch} at {head_sha}",
        pull_base_sha(pr, number),
    )


def pull_base_sha(payload: dict[str, Any], number: int) -> str:
    pull = payload.get("pull_request", payload)
    base = pull.get("base") if isinstance(pull, dict) else None
    sha = base.get("sha") if isinstance(base, dict) else None
    return validated_object_id(sha, f"PR #{number} base")


def validated_object_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not OBJECT_ID.fullmatch(value):
        raise ValueError(f"{label} is not an exact Git object ID")
    return value


def ensure_commit(workspace: Path, sha: str, token: str) -> None:
    if not git(
        "cat-file", "-e", f"{sha}^{{commit}}", cwd=workspace, check=False
    ).returncode:
        return
    with git_auth(token, workspace.parent) as auth_env:
        git("fetch", "--no-tags", "origin", sha, cwd=workspace, env=auth_env)


def clone_workspace(
    *,
    runner_workspace: Path,
    destination: Path,
    repository: str,
    token: str,
    remote_url: str | None = None,
) -> str:
    if destination.exists():
        raise ValueError(f"agent workspace already exists: {destination}")
    with git_auth(token, destination.parent) as env:
        run(
            [
                "/usr/bin/git",
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                remote_url or f"https://github.com/{repository}.git",
                str(destination),
            ],
            env=env,
        )

    base_sha = git(
        "rev-parse", "HEAD", cwd=runner_workspace, capture=True
    ).stdout.strip()
    # A full clone normally contains the exact trusted checkout commit. Fetch it
    # explicitly when the event names an object outside advertised refs.
    ensure_commit(destination, base_sha, token)
    return base_sha


def main() -> int:
    try:
        runner_workspace = Path(required("GITHUB_WORKSPACE")).resolve(strict=True)
        runner_temp = Path(required("RUNNER_TEMP")).resolve(strict=True)
        repository = required("GITHUB_REPOSITORY")
        token = required("GITHUB_TOKEN")
        github_env = Path(required("GITHUB_ENV"))
        mode = os.environ.get("TEND_CHECKOUT_MODE", "base")
        base_branch = os.environ.get("TEND_BASE_BRANCH") or required(
            "GITHUB_REPOSITORY_DEFAULT_BRANCH"
        )
        if mode not in {"base", "review", "mention"}:
            raise ValueError(f"unsupported checkout mode: {mode}")
        destination = (
            runner_temp
            / f"tend-agent-workspace-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
        )
        if destination == runner_workspace or destination.is_relative_to(
            runner_workspace
        ):
            raise ValueError(
                "agent workspace must be independent of the runner checkout"
            )

        base_sha = clone_workspace(
            runner_workspace=runner_workspace,
            destination=destination,
            repository=repository,
            token=token,
        )
        if mode == "base":
            selected = checkout_base(destination, base_branch, base_sha)
            config_base_sha = ""
        else:
            payload = event_payload()
            number = event_number(payload)
            if mode == "review":
                selected = checkout_review(destination, number, token)
                config_base_sha = pull_base_sha(payload, number)
            elif os.environ.get(
                "GITHUB_EVENT_NAME"
            ) == "issue_comment" and not issue_comment_targets_pull_request(payload):
                selected = checkout_base(destination, base_branch, base_sha)
                config_base_sha = ""
            else:
                selected, config_base_sha = checkout_mention(
                    destination,
                    repository=repository,
                    number=number,
                    token=token,
                    base_branch=base_branch,
                    base_sha=base_sha,
                )
            if config_base_sha:
                ensure_commit(destination, config_base_sha, token)

        git(
            "config",
            "--local",
            "--unset-all",
            "http.https://github.com/.extraheader",
            cwd=destination,
            check=False,
        )
        alternates = destination / ".git/objects/info/alternates"
        if alternates.exists():
            raise ValueError("agent clone unexpectedly shares a Git object store")
        origin = git(
            "remote", "get-url", "origin", cwd=destination, capture=True
        ).stdout
        if "@github.com" in origin or "x-access-token" in origin:
            raise ValueError("agent clone persisted a credential in its origin URL")

        with github_env.open("a", encoding="utf-8") as stream:
            stream.write(f"TEND_RUNNER_WORKSPACE={runner_workspace}\n")
            stream.write(f"TEND_AGENT_WORKSPACE={destination}\n")
            stream.write(f"TEND_CONFIG_BASE_SHA={config_base_sha}\n")
        log(f"prepared {destination}: {selected}")
        return 0
    except (OSError, subprocess.CalledProcessError, ValueError) as problem:
        return fail(str(problem))


if __name__ == "__main__":
    raise SystemExit(main())
