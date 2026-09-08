"""Event-topology contracts for the disposable agent checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import prepare_agent_workspace as prepare


def command(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path, str, str]:
    origin = tmp_path / "origin.git"
    command("init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)
    runner = tmp_path / "runner"
    command("init", "--initial-branch=main", str(runner), cwd=tmp_path)
    command("config", "user.email", "test@example.com", cwd=runner)
    command("config", "user.name", "Test", cwd=runner)
    (runner / "file").write_text("base\n")
    command("add", "file", cwd=runner)
    command("commit", "-m", "base", cwd=runner)
    base = command("rev-parse", "HEAD", cwd=runner)
    command("remote", "add", "origin", str(origin), cwd=runner)
    command("push", "-u", "origin", "main", cwd=runner)
    command("checkout", "-b", "feature", cwd=runner)
    (runner / "file").write_text("feature\n")
    command("commit", "-am", "feature", cwd=runner)
    head = command("rev-parse", "HEAD", cwd=runner)
    command("push", "origin", "feature", cwd=runner)
    command("push", "origin", f"{head}:refs/pull/7/head", cwd=runner)
    command("checkout", "main", cwd=runner)
    return origin, runner, base, head


def test_clone_is_independent_and_review_falls_back_to_head(tmp_path: Path) -> None:
    origin, runner, base, _head = repository(tmp_path)
    destination = tmp_path / "agent"

    assert (
        prepare.clone_workspace(
            runner_workspace=runner,
            destination=destination,
            repository="owner/repo",
            token="unused",
            remote_url=str(origin),
        )
        == base
    )
    assert not (destination / ".git/objects/info/alternates").exists()
    assert prepare.checkout_review(destination, 7) == "PR #7 head ref"
    assert (destination / "file").read_text() == "feature\n"
    assert subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=destination, check=False
    ).returncode == 1


def test_review_checkout_does_not_inherit_runner_git_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, runner, _base, _head = repository(tmp_path)
    command("checkout", "feature", cwd=runner)
    (runner / ".gitattributes").write_text("file filter=runner-hook\n")
    command("add", ".gitattributes", cwd=runner)
    command("commit", "-m", "attributes", cwd=runner)
    head = command("rev-parse", "HEAD", cwd=runner)
    command("push", "--force", "origin", f"{head}:refs/pull/7/head", cwd=runner)
    hook = tmp_path / "runner-filter"
    hook.write_text('#!/bin/sh\ncat\n: > "$0.ran"\n')
    hook.chmod(0o755)
    runner_config = tmp_path / "runner.gitconfig"
    command("config", "--file", str(runner_config), "filter.runner-hook.smudge", str(hook), cwd=tmp_path)
    command("config", "--file", str(runner_config), "filter.runner-hook.required", "true", cwd=tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(runner_config))
    destination = tmp_path / "agent"

    prepare.clone_workspace(
        runner_workspace=runner,
        destination=destination,
        repository="owner/repo",
        token="unused",
        remote_url=str(origin),
    )
    assert prepare.checkout_review(destination, 7) == "PR #7 head ref"

    assert not Path(f"{hook}.ran").exists()
    assert (destination / "file").read_text() == "feature\n"


def test_mention_checks_out_the_api_head_with_a_push_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, runner, base, head = repository(tmp_path)
    destination = tmp_path / "agent"
    prepare.clone_workspace(
        runner_workspace=runner,
        destination=destination,
        repository="owner/repo",
        token="unused",
        remote_url=str(origin),
    )
    monkeypatch.setattr(
        prepare,
        "api_json",
        lambda _path, _token: {
            "state": "open",
            "base": {"sha": base},
            "head": {
                "ref": "feature",
                "sha": head,
                "repo": {"clone_url": str(origin), "full_name": "fork/repo"},
            },
        },
    )

    selected, config_base = prepare.checkout_mention(
        destination,
        repository="owner/repo",
        number=7,
        token="unused",
        base_branch="main",
        base_sha=base,
    )

    assert selected == f"open PR #7 head fork/repo:feature at {head}"
    assert config_base == base
    assert command("rev-parse", "HEAD", cwd=destination) == head
    assert command("rev-parse", "@{upstream}", cwd=destination) == head


@pytest.mark.parametrize("sha", ["", "main", "-" * 40, "a" * 39, "a" * 41])
def test_pull_base_requires_an_exact_object_id(sha: str) -> None:
    with pytest.raises(ValueError, match="exact Git object ID"):
        prepare.pull_base_sha({"base": {"sha": sha}}, 7)


@pytest.mark.parametrize(
    ("payload", "number"),
    [
        ({"number": 1}, 1),
        ({"issue": {"number": 2}}, 2),
        ({"client_payload": {"pr": "3"}}, 3),
    ],
)
def test_event_number(payload: dict[str, object], number: int) -> None:
    assert prepare.event_number(payload) == number


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"issue": {"number": 1}}, False),
        ({"issue": {"number": 1, "pull_request": {"url": "example"}}}, True),
        ({}, False),
    ],
)
def test_issue_comment_targets_pull_request(
    payload: dict[str, object], expected: bool
) -> None:
    assert prepare.issue_comment_targets_pull_request(payload) is expected
