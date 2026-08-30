"""Behavior tests for review-preflight.sh."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts" / "review-preflight.sh"

BOT = "tend-bot"
PR = "7"
DRAFT_REVIEW_LINE = (
    "Reviewing as a draft — flagging anything that looks worth a quick fix. "
    "Mark ready for a full review."
)

FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$*" in
  "api user"*)              emit '{"login":"'"$BOT_LOGIN"'"}' ;;
  "pr view "*)              emit "$(cat "$PR_JSON")" ;;
  *"/pulls/"*"/comments"*)  emit "$(cat "$INLINE_JSON")" ;;
  *"/issues/"*"/timeline"*) emit "$(cat "$TIMELINE_JSON")" ;;
  *"/pulls/"*"/reviews"*)   emit "$(cat "$REVIEWS_JSON")" ;;
  *) exit 1 ;;
esac
"""
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), **GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.origin = tmp_path / "origin"
        self.work = tmp_path / "work"
        self.pin = tmp_path / "reviewed-head"

        git = shutil.which("git")
        assert git, "git is required for these tests"
        self.bindir = fake_bin(tmp_path, gh=FAKE_GH)
        self.git = Path(git)
        self.git_dir = Path(git).parent

        self.origin.mkdir()
        _git(self.origin, "init", "-b", "main", "-q")
        _git(self.origin, "config", "uploadpack.allowReachableSHA1InWant", "true")
        _commit(self.origin, "base.txt", "1\n", "base-1")
        _git(self.origin, "checkout", "-q", "-b", "pr")
        self.reviewed = _commit(self.origin, "feature.txt", "a\n", "pr-1")
        self.base = _git(self.origin, "rev-parse", "main")

        _git(tmp_path, "clone", "-q", str(self.origin), str(self.work))
        self.pin.write_text(self.reviewed + "\n")
        self.set_head(self.reviewed)

    def publish(self, sha: str) -> None:
        _git(self.origin, "update-ref", f"refs/pull/{PR}/head", sha)
        self.set_head(sha)

    def push_over_base_merge(self) -> str:
        _git(self.origin, "checkout", "-q", "main")
        self.base = _commit(self.origin, "base.txt", "2\n", "base-2")
        _git(self.origin, "checkout", "-q", "pr")
        _git(self.origin, "merge", "--no-ff", "-q", "-m", "Merge main into pr", "main")
        head = _commit(self.origin, "feature.txt", "a\nb\n", "pr-2")
        self.publish(head)
        return head

    def move_head_to_base(self) -> str:
        _git(self.origin, "checkout", "-q", "main")
        _git(self.origin, "merge", "--ff-only", "-q", "pr")
        self.base = _commit(self.origin, "base.txt", "2\n", "base-2")
        _git(self.origin, "checkout", "-q", "-B", "pr", "main")
        self.publish(self.base)
        return self.base

    def force_push(self) -> str:
        _git(self.origin, "checkout", "-q", "-B", "pr", "main")
        head = _commit(self.origin, "feature.txt", "rewritten\n", "pr-1'")
        self.publish(head)
        return head

    def set_head(self, sha: str, state: str = "OPEN") -> None:
        self.head = sha
        self.write(
            "PR_JSON", {"headRefOid": sha, "state": state, "baseRefOid": self.base}
        )

    def reviews(self, *reviews: dict) -> None:
        self.write("REVIEWS_JSON", list(reviews))

    def write(self, key: str, value: object) -> None:
        (self.tmp_path / f"{key.lower()}.json").write_text(json.dumps(value))

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": tool_path(self.bindir, self.git_dir),
            "HOME": str(self.tmp_path),
            "TMPDIR": str(self.tmp_path),
            "GH_CALLS": str(self.tmp_path / "gh-calls.log"),
            "GITHUB_REPOSITORY": "owner/repo",
            "BOT_LOGIN": BOT,
            "REVIEWED_HEAD_FILE": str(self.pin),
            "PR_JSON": str(self.tmp_path / "pr_json.json"),
            "INLINE_JSON": str(self.tmp_path / "inline_json.json"),
            "TIMELINE_JSON": str(self.tmp_path / "timeline_json.json"),
            "REVIEWS_JSON": str(self.tmp_path / "reviews_json.json"),
            **GIT_ENV,
            **extra,
        }

    def run(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(PREFLIGHT), PR],
            cwd=self.work,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )

    def output(self, **extra: str) -> str:
        result = self.run(**extra)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def pinned(self) -> str:
        return self.pin.read_text().strip()


@pytest.fixture
def pr(tmp_path: Path) -> Fixture:
    fixture = Fixture(tmp_path)
    fixture.write("INLINE_JSON", [])
    fixture.write("TIMELINE_JSON", [])
    fixture.reviews()
    return fixture


def _review(sha: str, body: str, state: str = "COMMENTED") -> dict:
    return {
        "id": 1,
        "user": {"login": BOT},
        "body": body,
        "state": state,
        "commit_id": sha,
        "submitted_at": "2026-01-01T00:00:00Z",
    }


def _event(path: Path, action: str) -> str:
    path.write_text(json.dumps({"action": action}))
    return str(path)


def _delta(output: str) -> str:
    paths = [
        line.removeprefix("delta: ")
        for line in output.splitlines()
        if line.startswith("delta: ")
    ]
    assert len(paths) == 1, output
    return Path(paths[0]).read_text()


def test_an_unreviewed_unchanged_head_posts(pr: Fixture) -> None:
    assert pr.output() == f"post: {pr.reviewed} is still the head you reviewed\n"
    assert pr.pinned() == pr.reviewed


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_closed_pr_is_skipped(pr: Fixture, state: str) -> None:
    pr.set_head(pr.head, state)

    assert pr.output() == f"skip: PR is {state}\n"


def test_a_moved_head_is_retargeted_with_a_scoped_delta(pr: Fixture) -> None:
    moved = pr.push_over_base_merge()

    output = pr.output()
    status = output.splitlines()[0]
    delta = _delta(output)

    assert status == f"post: re-targeted onto {moved} — read the delta before posting"
    assert pr.pinned() == moved
    assert "pr-2" in delta
    assert "base-2" not in delta
    assert "base merge: " in delta
    assert "Merge main into pr" in delta


def test_a_large_delta_is_preserved_for_chunked_reads(pr: Fixture) -> None:
    pr.push_over_base_merge()
    big = "\n".join(f"line {i} of a regenerated file" for i in range(8000))
    head = _commit(pr.origin, "generated.txt", big, "pr-3")
    pr.publish(head)

    output = pr.output()
    delta = _delta(output)

    assert len(delta) > 200_000
    assert output.startswith(f"post: re-targeted onto {head}")
    assert pr.pinned() == head


def test_a_failing_scoped_log_aborts(pr: Fixture) -> None:
    pr.push_over_base_merge()
    pr.base = "0" * 40
    pr.set_head(pr.head)

    result = pr.run()

    assert result.returncode != 0
    assert result.stdout == ""


def test_a_moved_head_can_have_an_empty_delta(pr: Fixture) -> None:
    moved = pr.move_head_to_base()

    output = pr.output()

    assert output.startswith(
        f"post: re-targeted onto {moved} — read the delta before posting\n"
    )
    assert _delta(output) == ""
    assert pr.pinned() == moved


def test_a_force_push_is_left_to_the_queued_review(pr: Fixture) -> None:
    rewritten = pr.force_push()

    output = pr.output()

    assert output.startswith(f"skip: cannot re-target onto {rewritten}")
    assert pr.pinned() == pr.reviewed


def test_a_merge_base_error_aborts(pr: Fixture) -> None:
    pr.push_over_base_merge()
    fake_git = pr.bindir / "git"
    fake_git.write_text(
        f'''#!/usr/bin/env bash
if [ "$1" = "merge-base" ]; then exit 128; fi
exec "{pr.git}" "$@"
'''
    )
    fake_git.chmod(0o755)

    result = pr.run()

    assert result.returncode == 128
    assert "git merge-base failed with status 128" in result.stderr
    assert result.stdout == ""
    assert pr.pinned() == pr.reviewed


def test_an_existing_review_stops_a_duplicate(pr: Fixture) -> None:
    pr.reviews(_review(pr.reviewed, "findings"))

    assert "already carries" in pr.output()


def test_dedup_uses_the_retargeted_head(pr: Fixture) -> None:
    moved = pr.push_over_base_merge()
    pr.reviews(_review(moved, "findings from the queued run"))

    output = pr.output()

    assert f"{moved} already carries" in output
    assert pr.pinned() == pr.reviewed


@pytest.mark.parametrize(
    ("action", "body", "expected"),
    [
        ("ready_for_review", DRAFT_REVIEW_LINE, "post:"),
        ("ready_for_review", "A landing concern.", "skip:"),
        ("synchronize", DRAFT_REVIEW_LINE, "skip:"),
    ],
)
def test_only_ready_for_review_replaces_a_draft_review(
    pr: Fixture, tmp_path: Path, action: str, body: str, expected: str
) -> None:
    pr.reviews(_review(pr.reviewed, body))

    output = pr.output(GITHUB_EVENT_PATH=_event(tmp_path / "event.json", action))

    assert output.startswith(expected)


def test_an_unreadable_pr_fails(pr: Fixture) -> None:
    Path(pr.env()["PR_JSON"]).write_text("")

    result = pr.run()

    assert result.returncode != 0
    assert "could not read PR" in result.stderr
    assert result.stdout == ""


def test_a_failing_review_state_check_preserves_the_delta_for_retry(
    pr: Fixture,
) -> None:
    moved = pr.push_over_base_merge()
    Path(pr.env()["REVIEWS_JSON"]).write_text("")

    result = pr.run()

    assert result.returncode != 0
    assert result.stdout == ""
    assert pr.pinned() == pr.reviewed

    pr.reviews()
    output = pr.output()

    assert output.startswith(f"post: re-targeted onto {moved}")
    assert "pr-2" in _delta(output)
    assert pr.pinned() == moved


def test_a_failed_status_write_preserves_the_delta_for_retry(pr: Fixture) -> None:
    moved = pr.push_over_base_merge()

    result = subprocess.run(
        [BASH, "-c", 'exec 1>&-; exec "$@"', "_", BASH, str(PREFLIGHT), PR],
        cwd=pr.work,
        env=pr.env(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert pr.pinned() == pr.reviewed

    output = pr.output()

    assert output.startswith(f"post: re-targeted onto {moved}")
    assert "pr-2" in _delta(output)
    assert pr.pinned() == moved


@pytest.mark.parametrize("content", ["", "\n", "head", None])
def test_an_invalid_pin_file_fails(pr: Fixture, content: str | None) -> None:
    if content is None:
        pr.pin.unlink()
    else:
        pr.pin.write_text(content)

    result = pr.run()

    assert result.returncode != 0
    assert "does not hold a commit sha" in result.stderr
    assert result.stdout == ""
