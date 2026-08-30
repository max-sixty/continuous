"""Tests for review-preflight.sh — the three checks that gate a posted review.

The recipe used to be forty lines of shell inside the review skill, retyped
from prose every session. Each check decides an outward action and none of
them fails loudly when it is skipped: an approval lands on a closed PR, a
review claims code the session never read, or a second review duplicates one
already on the commit. These pin the extracted script against a real git
repository, because two of the three answers come from git rather than the
API — what a base merge dragged in, and whether the push was a rewrite.
"""

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

# One fixture serves every `pr view`: the script's own `--jq` selects the
# fields, so a filter naming the wrong one reads as empty rather than passing.
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
    """A PR at `reviewed`, plus the pushes a mid-review head move can be."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.origin = tmp_path / "origin"
        self.work = tmp_path / "work"
        self.pin = tmp_path / "reviewed-head"

        # Built once: `fake_bin` creates its directory, so a second call from a
        # test that runs the script twice would fail on the existing one.
        git = shutil.which("git")
        assert git, "git is required for these tests"
        self.git_dir = Path(git).parent
        self.bindir = fake_bin(tmp_path, gh=FAKE_GH)

        self.origin.mkdir()
        _git(self.origin, "init", "-b", "main", "-q")
        # Real GitHub serves a reachable object by sha; a bare local remote
        # refuses unless asked to, and the base-tip fetch is one of those.
        _git(self.origin, "config", "uploadpack.allowReachableSHA1InWant", "true")
        _commit(self.origin, "base.txt", "1\n", "base-1")
        _git(self.origin, "checkout", "-q", "-b", "pr")
        self.reviewed = _commit(self.origin, "feature.txt", "a\n", "pr-1")
        self.base = _git(self.origin, "rev-parse", "main")

        # The session's checkout: it has the reviewed commit and nothing since.
        _git(tmp_path, "clone", "-q", str(self.origin), str(self.work))
        self.pin.write_text(self.reviewed + "\n")
        self.set_head(self.reviewed)

    def _publish(self, sha: str) -> None:
        _git(self.origin, "update-ref", f"refs/pull/{PR}/head", sha)

    def push_over_a_base_merge(self) -> str:
        """An "Update branch" click, then a commit of the author's own."""
        _git(self.origin, "checkout", "-q", "main")
        self.base = _commit(self.origin, "base.txt", "2\n", "base-2")
        _git(self.origin, "checkout", "-q", "pr")
        _git(self.origin, "merge", "--no-ff", "-q", "-m", "Merge main into pr", "main")
        head = _commit(self.origin, "feature.txt", "a\nb\n", "pr-2")
        self._publish(head)
        self.set_head(head)
        return head

    def base_absorbs_the_branch(self) -> str:
        """The base fast-forwards over the reviewed commit and moves on, and
        the PR head follows it: the head moved, but every commit it gained
        belongs to the base."""
        _git(self.origin, "checkout", "-q", "main")
        _git(self.origin, "merge", "--ff-only", "-q", "pr")
        self.base = _commit(self.origin, "base.txt", "2\n", "base-2")
        _git(self.origin, "checkout", "-q", "-B", "pr", "main")
        self._publish(self.base)
        self.set_head(self.base)
        return self.base

    def force_push(self) -> str:
        """A rewrite: the reviewed commit is no longer an ancestor."""
        _git(self.origin, "checkout", "-q", "-B", "pr", "main")
        head = _commit(self.origin, "feature.txt", "rewritten\n", "pr-1'")
        self._publish(head)
        self.set_head(head)
        return head

    # -- API fixtures --------------------------------------------------------

    def set_head(self, sha: str, state: str = "OPEN") -> None:
        self.head = sha
        self._write(
            "PR_JSON", {"headRefOid": sha, "state": state, "baseRefOid": self.base}
        )

    def close(self, state: str = "CLOSED") -> None:
        self.set_head(self.head, state)

    def reviews(self, *reviews: dict) -> None:
        self._write("REVIEWS_JSON", list(reviews))

    def _write(self, key: str, value: object) -> None:
        (self.tmp_path / f"{key.lower()}.json").write_text(json.dumps(value))

    # -- running -------------------------------------------------------------

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": tool_path(self.bindir, self.git_dir),
            "HOME": str(self.tmp_path),
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

    def verdict(self, **extra: str) -> dict:
        result = self.run(**extra)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def pinned(self) -> str:
        return self.pin.read_text().strip()


@pytest.fixture
def pr(tmp_path: Path) -> Fixture:
    fixture = Fixture(tmp_path)
    fixture._write("INLINE_JSON", [])
    fixture._write("TIMELINE_JSON", [])
    fixture.reviews()
    return fixture


def _review(
    rid: int,
    *,
    sha: str,
    body: str = "",
    state: str = "COMMENTED",
    at: str = "2026-01-01T00:00:00Z",
    author: str = BOT,
) -> dict:
    return {
        "id": rid,
        "user": {"login": author},
        "body": body,
        "state": state,
        "commit_id": sha,
        "submitted_at": at,
    }


def _event(path: Path, action: str) -> str:
    path.write_text(json.dumps({"action": action}))
    return str(path)


# ---------------------------------------------------------------------------
# The plain path
# ---------------------------------------------------------------------------


def test_an_unreviewed_head_that_has_not_moved_posts(pr: Fixture) -> None:
    verdict = pr.verdict()

    assert verdict["verdict"] == "post"
    assert verdict["head"] == pr.reviewed
    assert verdict["retargeted"] is False
    assert verdict["delta"] == ""
    assert pr.pinned() == pr.reviewed


def test_the_head_it_reports_is_the_head_the_pin_file_holds(pr: Fixture) -> None:
    """The two travel to the POST separately — `head` is what the session
    reads, the file is what the posting recipe substitutes — so a disagreement
    would pin a review to a commit the session never decided on."""
    moved = pr.push_over_a_base_merge()

    assert pr.verdict()["head"] == pr.pinned() == moved


# ---------------------------------------------------------------------------
# The PR closed under the session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_pr_that_closed_mid_review_takes_no_review(pr: Fixture, state: str) -> None:
    """HEAD does not move when a PR closes, so nothing else notices: the
    approval lands timestamped after the close."""
    pr.close(state)

    verdict = pr.verdict()

    assert verdict["verdict"] == "skip"
    assert state in verdict["reason"]


# ---------------------------------------------------------------------------
# A push mid-review
# ---------------------------------------------------------------------------


def test_a_moved_head_is_retargeted_and_the_pin_file_follows(pr: Fixture) -> None:
    moved = pr.push_over_a_base_merge()

    verdict = pr.verdict()

    assert verdict["verdict"] == "post"
    assert verdict["retargeted"] is True
    assert verdict["head"] == moved
    assert pr.pinned() == moved


def test_the_delta_is_the_authored_push_and_not_the_base_churn(pr: Fixture) -> None:
    """`--not "$BASE_SHA"` is what separates them. Without it the base branch's
    own commits sit in the range and read as the author's, and the session
    reviews main's history as if it were the push."""
    pr.push_over_a_base_merge()

    delta = pr.verdict()["delta"]

    assert "pr-2" in delta
    assert "base-2" not in delta, delta


def test_a_base_merge_appears_only_as_a_labelled_merges_line(pr: Fixture) -> None:
    """The scoped log drops the merge commit and everything it brought in, so
    an "Update branch" click is invisible there while it re-scopes every
    file's hunks. The label is what stops it reading as one more commit."""
    pr.push_over_a_base_merge()

    delta = pr.verdict()["delta"]

    assert "base merge: " in delta
    assert "Merge main into pr" in delta


def test_a_delta_larger_than_one_argv_string_still_reaches_the_session(
    pr: Fixture,
) -> None:
    """Linux caps a single argv string at 131072 bytes, so a patch passed to
    `jq --arg` kills the preflight outright — after the pin file has been
    advanced, which leaves the head this session never reviewed looking like
    the one it did. macOS has no per-argument cap, so this catches the
    regression on CI (ubuntu) rather than on a developer's box."""
    pr.push_over_a_base_merge()
    big = "\n".join(f"line {i} of a regenerated file" for i in range(8000))
    _commit(pr.origin, "generated.txt", big, "pr-3")
    head = _git(pr.origin, "rev-parse", "HEAD")
    _git(pr.origin, "update-ref", f"refs/pull/{PR}/head", head)
    pr.set_head(head)

    verdict = pr.verdict()

    assert len(verdict["delta"]) > 200_000, len(verdict["delta"])
    assert verdict["verdict"] == "post"
    assert pr.pinned() == head


def test_a_failing_scoped_log_aborts_rather_than_reporting_a_base_merge(
    pr: Fixture,
) -> None:
    """`set -e` does not reach inside `$( … )`, so the two logs have to run
    somewhere it does. Swallowed, the scoped log's failure leaves a delta of
    base merges alone — the author's push reading as an "Update branch" click,
    which the bullets treat as nothing new to review."""
    pr.push_over_a_base_merge()
    pr.base = "0" * 40  # a base tip that will not fetch or resolve
    pr.set_head(pr.head)

    result = pr.run()

    assert result.returncode != 0
    assert result.stdout == ""


def test_a_moved_head_can_carry_an_empty_delta(pr: Fixture) -> None:
    """`retargeted` is a separate field because an empty delta does not mean
    the head stayed put: `--not "$BASE_SHA"` legitimately empties it when
    everything the head gained is the base's. Read off `delta` alone, the
    session would post against a commit it never noticed it had moved to."""
    moved = pr.base_absorbs_the_branch()

    verdict = pr.verdict()

    assert verdict["retargeted"] is True
    assert verdict["delta"] == ""
    assert verdict["head"] == moved


def test_a_rewritten_head_is_left_to_the_queued_review(pr: Fixture) -> None:
    """Re-targeting needs the live head to build on the reviewed one. After a
    rewrite the findings describe code that is gone, and posting them against
    the new head would attach them to lines nobody wrote."""
    rewritten = pr.force_push()

    verdict = pr.verdict()

    assert verdict["verdict"] == "skip"
    assert "no longer an ancestor" in verdict["reason"]
    assert pr.pinned() == pr.reviewed, "the pin must not follow a rewrite"
    assert rewritten not in pr.pinned()


# ---------------------------------------------------------------------------
# A review already anchoring the head
# ---------------------------------------------------------------------------


def test_a_review_already_on_the_head_stops_a_duplicate(pr: Fixture) -> None:
    pr.reviews(_review(1, sha=pr.reviewed, body="findings"))

    verdict = pr.verdict()

    assert verdict["verdict"] == "skip"
    assert "already carries" in verdict["reason"]


def test_the_dedup_reads_the_head_the_review_was_retargeted_onto(
    pr: Fixture,
) -> None:
    """A racing run can post at the new head while this one is composing, so
    the check has to run against the head this review would now pin — not the
    one the session started from."""
    moved = pr.push_over_a_base_merge()
    pr.reviews(_review(1, sha=moved, body="findings from the queued run"))

    assert pr.verdict()["verdict"] == "skip"
    # The re-target already happened, so the pin follows the head even though
    # nothing is posted. Harmless only because a skip ends the session — worth
    # stating, since a later path that posted after a skip would post unpinned.
    assert pr.pinned() == moved


def test_a_reply_container_on_the_head_does_not_stop_the_post(pr: Fixture) -> None:
    """Deferred to `bot-review-state.sh`: replying to a thread makes GitHub
    wrap the reply in a zero-body COMMENTED review anchored at the head. Read
    as a review it would discard this session's real one."""
    pr.reviews(_review(1, sha=pr.reviewed))

    assert pr.verdict()["verdict"] == "post"


def test_a_ready_for_review_event_replaces_the_draft_comment(
    pr: Fixture, tmp_path: Path
) -> None:
    """Becoming ready asks for the full review the draft pass withheld."""
    pr.reviews(_review(1, sha=pr.reviewed, body=DRAFT_REVIEW_LINE))

    verdict = pr.verdict(
        GITHUB_EVENT_PATH=_event(tmp_path / "event.json", "ready_for_review")
    )

    assert verdict["verdict"] == "post"


def test_a_ready_for_review_event_still_stops_on_a_full_review(
    pr: Fixture, tmp_path: Path
) -> None:
    """The override reaches Tend's own draft COMMENT and nothing else — a full
    pass that raced this one still stands."""
    pr.reviews(_review(1, sha=pr.reviewed, body="A landing concern."))

    verdict = pr.verdict(
        GITHUB_EVENT_PATH=_event(tmp_path / "event.json", "ready_for_review")
    )

    assert verdict["verdict"] == "skip"


def test_a_synchronize_event_does_not_replace_the_draft_comment(
    pr: Fixture, tmp_path: Path
) -> None:
    pr.reviews(_review(1, sha=pr.reviewed, body=DRAFT_REVIEW_LINE))

    verdict = pr.verdict(
        GITHUB_EVENT_PATH=_event(tmp_path / "event.json", "synchronize")
    )

    assert verdict["verdict"] == "skip"


# ---------------------------------------------------------------------------
# The pin file
# ---------------------------------------------------------------------------


def test_an_unreadable_pr_fails_rather_than_reading_as_closed(pr: Fixture) -> None:
    """`gh` failing must not come back as a verdict. An empty state is not
    "OPEN", so a blip that went unnoticed would look like a considered
    decision to post nothing, and the review would be lost."""
    Path(pr.env()["PR_JSON"]).write_text("")

    result = pr.run()

    assert result.returncode != 0
    assert "could not read PR" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("content", ["", "\n", "head", None])
def test_a_pin_file_that_holds_no_sha_fails_rather_than_posting(
    pr: Fixture, content: str | None
) -> None:
    """Every posting recipe substitutes this file as the review's `commit_id`.
    Empty, GitHub anchors the review at whatever is live when the POST lands —
    the unpinned review the file exists to prevent — so an absent one is a bug
    in step 1, not a verdict. A lone newline survives a size test, and the
    empty sha it yields then reads downstream as a force-push."""
    if content is None:
        pr.pin.unlink()
    else:
        pr.pin.write_text(content)

    result = pr.run()

    assert result.returncode != 0
    assert "does not hold a commit sha" in result.stderr
    assert result.stdout == ""
