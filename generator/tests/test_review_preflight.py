"""Behavior tests for review_preflight.py."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts" / "review_preflight.py"

BOT = "tend-bot"
PR = "7"
DRAFT_REVIEW_MARKER = "<!-- tend:draft-review -->"
LEGACY_DRAFT_REVIEW_LINE = (
    "Reviewing as a draft — flagging anything that looks worth a quick fix. "
    "Mark ready for a full review."
)

FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$*" in
  "api user"*)              emit '{"login":"'"$BOT_LOGIN"'"}' ;;
  "pr view "*"--json headRefOid,state,baseRefOid"*)
                              emit "$(cat "$PR_JSON")" ;;
  "pr view "*"--json headRefOid,state,baseRefOid,author,isDraft"*)
                              emit "$(cat "$PR_JSON")" ;;
  "pr view "*"--json headRefOid,state"*)
                              emit "$(cat "$FINAL_PR_JSON")" ;;
  "pr view "*"--json headRefOid"*)
                              emit "$(cat "$BOT_HEAD_JSON")" ;;
  *"/reviews --method POST --input -"*)
                              cat > "$POST_INPUT"
                              if [ "${POST_EXIT:-0}" -ne 0 ]; then
                                printf '%s\n' "${POST_ERROR:-review failed}" >&2
                                exit "$POST_EXIT"
                              fi
                              emit "${POST_RESPONSE:-{\"id\":42}}" ;;
  *"/reviews/"*" --method PUT --input -"*)
                              cat > "$PUT_INPUT"
                              if [ "${PUT_EXIT:-0}" -ne 0 ]; then
                                printf '%s\n' "${PUT_ERROR:-edit failed}" >&2
                                exit "$PUT_EXIT"
                              fi
                              emit "${PUT_RESPONSE:-{\"id\":42}}" ;;
  *"/pulls/"*"/comments"*)  emit "$(cat "$INLINE_JSON")" ;;
  *"/issues/"*"/timeline"*) emit "$(cat "$TIMELINE_JSON")" ;;
  *"/pulls/"*"/reviews"*)
                              if [ "${REVIEWS_AFTER_POST_INVALID:-0}" -eq 1 ] && [ -e "$POST_INPUT" ]; then
                                emit ""
                              else
                                emit "$(cat "$REVIEWS_JSON")"
                              fi ;;
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
        self.context = tmp_path / "review-context.json"

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

    def set_head(
        self, sha: str, state: str = "OPEN", *, is_draft: bool = False
    ) -> None:
        self.head = sha
        view = {
            "headRefOid": sha,
            "state": state,
            "baseRefOid": self.base,
            "author": {"login": "author"},
            "isDraft": is_draft,
        }
        self.write("PR_JSON", view)
        self.write("FINAL_PR_JSON", view)
        self.write("BOT_HEAD_JSON", {"headRefOid": sha})

    def set_final(
        self, sha: str | None = None, state: str = "OPEN", is_draft: bool = False
    ) -> None:
        self.write(
            "FINAL_PR_JSON",
            {
                "headRefOid": sha or self.head,
                "state": state,
                "baseRefOid": self.base,
                "isDraft": is_draft,
            },
        )

    def set_resolver_head(self, sha: str) -> None:
        self.write("BOT_HEAD_JSON", {"headRefOid": sha})

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
            "REVIEW_CONTEXT_FILE": str(self.context),
            "PR_JSON": str(self.tmp_path / "pr_json.json"),
            "FINAL_PR_JSON": str(self.tmp_path / "final_pr_json.json"),
            "BOT_HEAD_JSON": str(self.tmp_path / "bot_head_json.json"),
            "INLINE_JSON": str(self.tmp_path / "inline_json.json"),
            "TIMELINE_JSON": str(self.tmp_path / "timeline_json.json"),
            "REVIEWS_JSON": str(self.tmp_path / "reviews_json.json"),
            "POST_INPUT": str(self.tmp_path / "post-input.json"),
            "PUT_INPUT": str(self.tmp_path / "put-input.json"),
            **GIT_ENV,
            **extra,
        }

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-E", "-s", str(PREFLIGHT), "post", PR, *args],
            cwd=self.work,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )

    def output(self, *args: str, **extra: str) -> str:
        result = self.run(*args, **extra)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def start(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-E", "-s", str(PREFLIGHT), "start", PR],
            cwd=self.work,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )

    def submit(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-E", "-s", str(PREFLIGHT), "submit", PR, *args],
            cwd=self.work,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )

    def complete(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-E", "-s", str(PREFLIGHT), "complete", PR],
            cwd=self.work,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
        )

    def submitted(self) -> dict:
        return json.loads((self.tmp_path / "post-input.json").read_text())

    def edited(self) -> dict:
        return json.loads((self.tmp_path / "put-input.json").read_text())

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


def test_start_records_one_snapshot_and_prepares_the_incremental(pr: Fixture) -> None:
    moved = pr.push_over_base_merge()
    pr.reviews(_review(pr.reviewed, "earlier finding"))

    result = pr.start()

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert context == {
        "head_sha": moved,
        "author": "author",
        "bot_login": BOT,
        "is_draft": False,
        "force_full_review": False,
        "last_review_sha": pr.reviewed,
        "force_pushed_since": False,
        "incremental_path": context["incremental_path"],
        "ready_review_event_id": None,
        "recovery_review_id": None,
        "operation_id": context["operation_id"],
    }
    assert re.fullmatch(r"[0-9a-f]{32}", context["operation_id"])
    assert json.loads(pr.context.read_text()) == {
        "operation_id": context["operation_id"],
        "full_non_draft": False,
        "ready_review_event_id": None,
        "recovery_review_id": None,
    }
    assert "pr-2" in Path(context["incremental_path"]).read_text()
    assert "base-2" not in Path(context["incremental_path"]).read_text()
    assert pr.pinned() == moved


def test_start_captures_the_exact_outstanding_readiness_generation(
    pr: Fixture,
) -> None:
    pr.reviews(_review(pr.reviewed, "Earlier pass."))
    pr.write(
        "TIMELINE_JSON",
        [
            {
                "id": 31,
                "event": "ready_for_review",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ],
    )

    result = pr.start()

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert context["force_full_review"] is True
    assert context["ready_review_event_id"] == 31
    assert context["incremental_path"] is None
    assert json.loads(pr.context.read_text())["ready_review_event_id"] == 31


def test_complete_durably_acknowledges_an_intentionally_silent_ready_pass(
    pr: Fixture,
) -> None:
    pr.write(
        "TIMELINE_JSON",
        [
            {
                "id": 31,
                "event": "ready_for_review",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ],
    )
    assert pr.start().returncode == 0

    result = pr.complete()

    assert result.returncode == 0, result.stderr
    assert result.stdout == "acknowledged: review 42\n"
    assert pr.submitted() == {
        "event": "COMMENT",
        "commit_id": pr.reviewed,
        "body": "<!-- tend:ready-review:31 -->",
    }

    pr.reviews(_review(pr.reviewed, "<!-- tend:ready-review:31 -->"))
    next_start = pr.start()
    assert next_start.returncode == 0, next_start.stderr
    assert json.loads(next_start.stdout)["force_full_review"] is False


def test_complete_is_a_no_op_without_captured_readiness(pr: Fixture) -> None:
    assert pr.start().returncode == 0

    result = pr.complete()

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "skip: no captured ready-for-review generation needs acknowledgment\n"
    )
    assert not (pr.tmp_path / "post-input.json").exists()


def test_start_does_not_capture_readiness_while_the_pr_is_draft(
    pr: Fixture,
) -> None:
    pr.set_head(pr.reviewed, is_draft=True)
    pr.reviews(_review(pr.reviewed, DRAFT_REVIEW_MARKER))
    pr.write(
        "TIMELINE_JSON",
        [
            {
                "id": 31,
                "event": "ready_for_review",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ],
    )

    result = pr.start()

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert context["force_full_review"] is False
    assert context["ready_review_event_id"] is None


def test_start_selects_only_a_mode_compatible_incomplete_review(
    pr: Fixture,
) -> None:
    pr.reviews(
        {
            **_review(
                pr.reviewed,
                "<!-- tend:review-incomplete:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->",
            ),
            "id": 77,
        },
        {
            **_review(
                pr.reviewed,
                "<!-- tend:review-incomplete:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb -->\n"
                "<!-- tend:ready-review:31 -->",
            ),
            "id": 88,
        },
    )

    result = pr.start()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["recovery_review_id"] == 77
    assert json.loads(pr.context.read_text())["recovery_review_id"] == 77


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


def test_a_head_change_during_state_resolution_skips(pr: Fixture) -> None:
    rewritten = pr.force_push()
    pr.set_head(pr.reviewed)
    pr.set_resolver_head(rewritten)

    assert pr.output() == f"skip: HEAD moved to {rewritten} during the preflight\n"
    assert pr.pinned() == pr.reviewed


def test_a_close_during_preflight_skips(pr: Fixture) -> None:
    pr.set_final(state="CLOSED")

    assert pr.output() == "skip: PR is CLOSED\n"
    assert pr.pinned() == pr.reviewed


def test_a_head_change_during_final_read_skips(pr: Fixture) -> None:
    rewritten = pr.force_push()
    pr.set_head(pr.reviewed)
    pr.set_final(rewritten)

    assert pr.output() == f"skip: HEAD moved to {rewritten} during the preflight\n"
    assert pr.pinned() == pr.reviewed


def test_submit_approves_only_the_stable_pinned_head(
    pr: Fixture, tmp_path: Path
) -> None:
    assert pr.start().returncode == 0

    result = pr.submit("--event", "APPROVE")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "posted: review 42\n"
    assert pr.submitted() == {
        "event": "APPROVE",
        "commit_id": pr.reviewed,
        "body": "",
    }

    moved = pr.push_over_base_merge()
    result = pr.submit("--event", "APPROVE")

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"skip: HEAD moved to {moved} before the outward action\n"


def test_submit_injects_only_the_captured_readiness_marker(
    pr: Fixture, tmp_path: Path
) -> None:
    event_path = _event(tmp_path / "event.json", "ready_for_review")
    pr.write(
        "TIMELINE_JSON",
        [{"id": 31, "event": "ready_for_review", "created_at": "2026-01-02T00:00:00Z"}],
    )
    start = pr.start(GITHUB_EVENT_PATH=event_path)
    assert start.returncode == 0, start.stderr
    body = tmp_path / "body.md"
    body.write_text(
        "Looks good.\n<!-- tend:ready-review:999 -->\n"
        "<!-- tend:review-incomplete:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->\n"
    )

    result = pr.submit("--event", "APPROVE", "--body-file", str(body))

    assert result.returncode == 0, result.stderr
    assert pr.submitted()["body"] == ("Looks good.\n\n<!-- tend:ready-review:31 -->")


def test_submit_preserves_draft_review_identity(pr: Fixture, tmp_path: Path) -> None:
    pr.set_head(pr.reviewed, is_draft=True)
    assert pr.start().returncode == 0
    body = tmp_path / "body.md"
    body.write_text("One early concern.")

    result = pr.submit("--event", "COMMENT", "--body-file", str(body))

    assert result.returncode == 0, result.stderr
    assert pr.submitted()["body"] == (
        "One early concern.\n\n<!-- tend:draft-review -->"
    )


def test_inline_submit_finalizes_only_after_the_comments_post(
    pr: Fixture, tmp_path: Path
) -> None:
    start = pr.start()
    assert start.returncode == 0, start.stderr
    context = json.loads(pr.context.read_text())
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "body": "One issue.",
                "comments": [{"path": "feature.txt", "line": 1, "body": "Fix it"}],
                "commit_id": "agent-supplied",
                "event": "APPROVE",
            }
        )
    )

    result = pr.submit("--event", "COMMENT", "--payload-file", str(payload))

    assert result.returncode == 0, result.stderr
    assert pr.submitted() == {
        "event": "COMMENT",
        "commit_id": pr.reviewed,
        "body": (f"<!-- tend:review-incomplete:{context['operation_id']} -->"),
        "comments": [{"path": "feature.txt", "line": 1, "body": "Fix it"}],
    }
    assert pr.edited() == {"body": "One issue."}


def test_incomplete_inline_record_carries_its_readiness_generation(
    pr: Fixture, tmp_path: Path
) -> None:
    pr.write(
        "TIMELINE_JSON",
        [
            {
                "id": 31,
                "event": "ready_for_review",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ],
    )
    assert pr.start().returncode == 0
    operation_id = json.loads(pr.context.read_text())["operation_id"]
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "body": "One issue.",
                "comments": [{"path": "feature.txt", "line": 1, "body": "Fix it"}],
            }
        )
    )

    result = pr.submit(
        "--event",
        "COMMENT",
        "--payload-file",
        str(payload),
        PUT_EXIT="22",
    )

    assert result.returncode == 22
    assert pr.submitted()["body"] == (
        f"<!-- tend:review-incomplete:{operation_id} -->\n\n"
        "<!-- tend:ready-review:31 -->"
    )


def test_partial_inline_failure_reports_its_operation_specific_review(
    pr: Fixture, tmp_path: Path
) -> None:
    start = pr.start()
    assert start.returncode == 0, start.stderr
    operation_id = json.loads(pr.context.read_text())["operation_id"]
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "body": "One issue.",
                "comments": [{"path": "feature.txt", "line": 1, "body": "Fix it"}],
            }
        )
    )
    pr.reviews(
        {
            **_review(
                pr.reviewed,
                f"<!-- tend:review-incomplete:{operation_id} -->",
            ),
            "id": 77,
        }
    )

    result = pr.submit(
        "--event",
        "COMMENT",
        "--payload-file",
        str(payload),
        POST_EXIT="22",
        POST_ERROR="line could not be resolved",
    )

    assert result.returncode == 22
    assert result.stdout == "recover: incomplete review 77\n"
    assert "line could not be resolved" in result.stderr


@pytest.mark.parametrize("reviews_json", [[], None])
def test_ambiguous_inline_failure_never_invites_a_second_post(
    pr: Fixture, tmp_path: Path, reviews_json: list[dict] | None
) -> None:
    assert pr.start().returncode == 0
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "body": "One issue.",
                "comments": [{"path": "feature.txt", "line": 1, "body": "Fix it"}],
            }
        )
    )
    if reviews_json is not None:
        pr.reviews(*reviews_json)

    result = pr.submit(
        "--event",
        "COMMENT",
        "--payload-file",
        str(payload),
        POST_EXIT="22",
        POST_ERROR="line could not be resolved",
        REVIEWS_AFTER_POST_INVALID="1" if reviews_json is None else "0",
    )

    assert result.returncode == 22
    assert result.stdout == "uncertain: review submission outcome unknown\n"
    assert "line could not be resolved" in result.stderr


def test_inline_finalize_failure_leaves_an_explicit_recovery_target(
    pr: Fixture, tmp_path: Path
) -> None:
    assert pr.start().returncode == 0
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "body": "One issue.",
                "comments": [{"path": "feature.txt", "line": 1, "body": "Fix it"}],
            }
        )
    )

    result = pr.submit(
        "--event",
        "COMMENT",
        "--payload-file",
        str(payload),
        PUT_EXIT="22",
        PUT_ERROR="temporary edit failure",
    )

    assert result.returncode == 22
    assert result.stdout == "recover: incomplete review 42\n"
    assert "temporary edit failure" in result.stderr


def test_submit_edits_only_an_incomplete_review_on_the_current_head(
    pr: Fixture, tmp_path: Path
) -> None:
    start = pr.start()
    assert start.returncode == 0, start.stderr
    operation_id = json.loads(pr.context.read_text())["operation_id"]
    body = tmp_path / "body.md"
    body.write_text("Recovered finding.")
    pr.reviews(
        {
            **_review(
                pr.reviewed,
                f"<!-- tend:review-incomplete:{operation_id} -->",
            ),
            "id": 77,
        }
    )

    result = pr.submit("--edit-review", "77", "--body-file", str(body))

    assert result.returncode == 0, result.stderr
    assert result.stdout == "posted: review 42\n"
    assert pr.edited() == {"body": "Recovered finding."}

    result = pr.submit("--edit-review", "999", "--body-file", str(body))
    assert result.returncode == 0, result.stderr
    assert result.stdout == (f"skip: {pr.reviewed} cannot edit incomplete review 999\n")


def test_finalized_coverage_makes_an_older_incomplete_review_inert(
    pr: Fixture, tmp_path: Path
) -> None:
    assert pr.start().returncode == 0
    operation_id = json.loads(pr.context.read_text())["operation_id"]
    body = tmp_path / "body.md"
    body.write_text("Recovered finding.")
    pr.reviews(
        {
            **_review(
                pr.reviewed,
                f"<!-- tend:review-incomplete:{operation_id} -->",
            ),
            "id": 77,
        },
        {**_review(pr.reviewed, "A later complete review."), "id": 88},
    )

    result = pr.submit("--edit-review", "77", "--body-file", str(body))

    assert result.returncode == 0, result.stderr
    assert result.stdout == (f"skip: {pr.reviewed} cannot edit incomplete review 77\n")
    assert not (tmp_path / "put-input.json").exists()


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
        ("ready_for_review", DRAFT_REVIEW_MARKER, "post:"),
        ("ready_for_review", LEGACY_DRAFT_REVIEW_LINE, "post:"),
        ("ready_for_review", "A landing concern.", "post:"),
        ("synchronize", DRAFT_REVIEW_MARKER, "skip:"),
    ],
)
def test_only_ready_for_review_replaces_a_same_head_review(
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
        [
            BASH,
            "-c",
            'exec 1>&-; exec "$@"',
            "_",
            sys.executable,
            "-E",
            "-s",
            str(PREFLIGHT),
            "post",
            PR,
        ],
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

    results = [
        pr.run(),
        pr.submit("--event", "APPROVE"),
        pr.complete(),
    ]

    for result in results:
        assert result.returncode != 0
        assert "does not hold a commit sha" in result.stderr
        assert result.stdout == ""
        assert "Traceback" not in result.stderr
