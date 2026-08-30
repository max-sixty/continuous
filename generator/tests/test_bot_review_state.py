"""Tests for bot-review-state.sh — which of the bot's reviews anchors the head.

Every field here decides an outward action: whether the bot re-reviews a
commit, whether it approves one it never read, whether it edits an existing
review or duplicates it, and whether a stale approval gets dismissed. The
logic used to be copy-pasted across five skill call sites, where the copies
had already drifted; these pin the single implementation they collapsed into.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

BOT_REVIEW_STATE = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "tend-ci-runner"
    / "scripts"
    / "bot-review-state.sh"
)
REVIEW_SKILL = BOT_REVIEW_STATE.parent.parent / "skills" / "review" / "SKILL.md"

BOT = "tend-bot"
HEAD = "head000"
DRAFT_REVIEW_LINE = (
    "Reviewing as a draft — flagging anything that looks worth a quick fix. "
    "Mark ready for a full review."
)
OLD = "old0000"

FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$*" in
  "api user"*)          emit '{"login":"'"$BOT_LOGIN"'"}' ;;
  "pr view "*)          emit "$(cat "$PR_HEAD_JSON")" ;;
  *"/pulls/"*"/comments"*) emit "$(cat "$INLINE_JSON")" ;;
  *"/issues/"*"/timeline"*) emit "$(cat "$TIMELINE_JSON")" ;;
  *"/pulls/"*"/reviews"*)   emit "$(cat "$REVIEWS_JSON")" ;;
  *) exit 1 ;;
esac
"""
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Fake `gh` plus fixtures for a PR at HEAD with no reviews and no rewrite."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH)
    files = {
        # `commits` is deliberately wrong-last: `gh pr view --json commits`
        # is `commits(first: 100)`, oldest-first, so past the cap `.commits[-1]`
        # is commit #100. A reader that goes back to it reads OLD here.
        "PR_HEAD_JSON": {
            "headRefOid": HEAD,
            "commits": [{"oid": HEAD}, {"oid": OLD}],
        },
        "INLINE_JSON": [],
        "TIMELINE_JSON": [],
        "REVIEWS_JSON": [],
    }
    paths = {}
    for key, value in files.items():
        path = tmp_path / f"{key.lower()}.json"
        path.write_text(json.dumps(value))
        paths[key] = str(path)
    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_REPOSITORY": "owner/repo",
        "BOT_LOGIN": BOT,
        **paths,
    }


def _state(env: dict[str, str]) -> dict:
    result = subprocess.run(
        [BASH, str(BOT_REVIEW_STATE), "7"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _write(env: dict[str, str], key: str, value: object) -> None:
    Path(env[key]).write_text(json.dumps(value))


def _review(
    rid: int,
    at: str | None,
    *,
    author: str = BOT,
    body: str = "",
    state: str = "COMMENTED",
    sha: str = HEAD,
) -> dict:
    return {
        "id": rid,
        "user": {"login": author},
        "body": body,
        "state": state,
        "commit_id": sha,
        "submitted_at": at,
    }


def _rewrite_at(env: dict[str, str], *times: str) -> None:
    _write(
        env,
        "TIMELINE_JSON",
        [{"event": "head_ref_force_pushed", "created_at": t} for t in times],
    )


# ---------------------------------------------------------------------------
# Substantive vs. synthetic reply containers
# ---------------------------------------------------------------------------


def test_a_clean_pr_reports_nothing_anchored(env: dict[str, str]) -> None:
    state = _state(env)

    assert state["head_sha"] == HEAD
    assert state["last_substantive"] is None
    assert state["at_head"] is None
    assert state["orphan_id"] is None
    assert state["fresh_approval_sha"] == ""
    assert state["stale_approval_id"] == ""
    assert state["force_pushed_since"] is False


def test_an_unsubmitted_review_anchors_nothing(env: dict[str, str]) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, None, body="draft findings", state="PENDING")],
    )

    state = _state(env)

    assert state["last_substantive"] is None
    assert state["at_head"] is None
    assert state["orphan_id"] is None


def test_a_reply_container_does_not_read_as_a_review(env: dict[str, str]) -> None:
    """Replying to a review thread makes GitHub wrap the reply in a zero-body
    COMMENTED review anchored at the then-current head. Counted, it would tell
    the next run this commit was already reviewed and discard a real review."""
    _write(env, "REVIEWS_JSON", [_review(1, "2026-01-01T00:00:00Z")])

    state = _state(env)

    assert state["last_substantive"] is None
    assert state["at_head"] is None


@pytest.mark.parametrize(
    ("kind", "review"),
    [
        ("a body", _review(1, "2026-01-01T00:00:00Z", body="findings")),
        ("an approval", _review(1, "2026-01-01T00:00:00Z", state="APPROVED")),
    ],
)
def test_a_review_with_content_anchors(
    env: dict[str, str], kind: str, review: dict
) -> None:
    """An empty-body approval still anchors — it is a verdict on this commit."""
    _write(env, "REVIEWS_JSON", [review])

    assert _state(env)["at_head"]["id"] == 1, kind


def test_at_head_identifies_a_tend_draft_review(env: dict[str, str]) -> None:
    """A ready-for-review pass may replace its earlier draft COMMENT, while
    ordinary duplicate runs still stop on every other substantive review."""
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                1,
                "2026-01-01T00:00:00Z",
                body=DRAFT_REVIEW_LINE,
            )
        ],
    )

    assert _state(env)["at_head"]["draft_mode"] is True


def test_at_head_does_not_call_an_ordinary_comment_draft_mode(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, "2026-01-01T00:00:00Z", body="One landing concern.")],
    )

    assert _state(env)["at_head"]["draft_mode"] is False


def test_a_review_owning_a_fresh_inline_comment_anchors(env: dict[str, str]) -> None:
    """An empty-body review carrying real inline findings is indistinguishable
    from a reply container by body alone; the top-level inline comment is what
    separates them."""
    _write(env, "REVIEWS_JSON", [_review(9, "2026-01-01T00:00:00Z")])
    _write(env, "INLINE_JSON", [{"in_reply_to_id": None, "pull_request_review_id": 9}])

    assert _state(env)["at_head"]["id"] == 9


def test_a_reply_only_inline_comment_does_not_promote_its_container(
    env: dict[str, str],
) -> None:
    _write(env, "REVIEWS_JSON", [_review(9, "2026-01-01T00:00:00Z")])
    _write(env, "INLINE_JSON", [{"in_reply_to_id": 4, "pull_request_review_id": 9}])

    assert _state(env)["at_head"] is None


def test_another_authors_review_is_not_ours(env: dict[str, str]) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, "2026-01-01T00:00:00Z", author="human", state="APPROVED")],
    )

    state = _state(env)

    assert state["at_head"] is None
    assert state["fresh_approval_sha"] == ""


# ---------------------------------------------------------------------------
# Force-push re-anchoring
# ---------------------------------------------------------------------------


def test_a_rewrite_after_the_review_invalidates_its_anchor(env: dict[str, str]) -> None:
    """GitHub re-points an earlier review's commit_id at the new head, so the
    anchor reads as current for code that was rewritten away. Without the flag
    the run exits silently and a stale APPROVE stands as the verdict."""
    _write(env, "REVIEWS_JSON", [_review(1, "2026-01-01T00:00:00Z", body="findings")])
    _rewrite_at(env, "2026-01-02T00:00:00Z")

    state = _state(env)

    assert state["force_pushed_since"] is True
    assert state["at_head"] is None, "a rewritten-away review must not gate a re-review"
    assert state["last_substantive"]["id"] == 1


def test_a_rewrite_before_the_review_leaves_it_standing(env: dict[str, str]) -> None:
    _write(env, "REVIEWS_JSON", [_review(1, "2026-01-03T00:00:00Z", body="findings")])
    _rewrite_at(env, "2026-01-02T00:00:00Z")

    state = _state(env)

    assert state["force_pushed_since"] is False
    assert state["at_head"]["id"] == 1


def test_the_newest_rewrite_is_the_one_that_counts(env: dict[str, str]) -> None:
    """Timeline order is not guaranteed, and an older rewrite would let a
    review submitted between the two read as still anchored."""
    _write(env, "REVIEWS_JSON", [_review(1, "2026-01-03T00:00:00Z", body="findings")])
    _rewrite_at(env, "2026-01-04T00:00:00Z", "2026-01-01T00:00:00Z")

    assert _state(env)["force_pushed_since"] is True


# ---------------------------------------------------------------------------
# Approvals: fresh vs. stale
# ---------------------------------------------------------------------------


def test_an_approval_that_predates_the_rewrite_is_stale(env: dict[str, str]) -> None:
    """A rebased dependency PR carries an approval it never earned; the skip
    paths comment that it isn't mergeable while the PR reads as bot-approved."""
    _write(env, "REVIEWS_JSON", [_review(3, "2026-01-01T00:00:00Z", state="APPROVED")])
    _rewrite_at(env, "2026-01-02T00:00:00Z")

    state = _state(env)

    assert state["stale_approval_id"] == 3
    assert state["fresh_approval_sha"] == "", "a rewritten-away approval is not earned"


def test_a_later_approval_supersedes_the_stale_one(env: dict[str, str]) -> None:
    """Newest-then-test, never test-then-newest: the question is whether the
    approval now setting the PR's state is stale. Filtering first would name
    the pre-rewrite id and leave the live approval standing while dismissing a
    review that no longer decides anything."""
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(3, "2026-01-01T00:00:00Z", state="APPROVED", sha=OLD),
            _review(4, "2026-01-03T00:00:00Z", state="APPROVED"),
        ],
    )
    _rewrite_at(env, "2026-01-02T00:00:00Z")

    state = _state(env)

    assert state["stale_approval_id"] == ""
    assert state["fresh_approval_sha"] == HEAD


def test_a_dismissed_approval_is_not_re_dismissed(env: dict[str, str]) -> None:
    """Dismissing sets the state to DISMISSED, which stops matching — `weekly`
    relies on that to keep a later run from dismissing the same review again."""
    _write(env, "REVIEWS_JSON", [_review(3, "2026-01-01T00:00:00Z", state="DISMISSED")])
    _rewrite_at(env, "2026-01-02T00:00:00Z")

    assert _state(env)["stale_approval_id"] == ""


def test_an_approval_on_an_older_commit_is_not_an_approval_of_this_one(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(3, "2026-01-01T00:00:00Z", state="APPROVED", sha=OLD)],
    )

    state = _state(env)

    assert state["fresh_approval_sha"] == OLD
    assert state["at_head"] is None


# ---------------------------------------------------------------------------
# Orphan bodies from a partially-failed review POST
# ---------------------------------------------------------------------------


def test_a_body_bearing_review_on_the_head_is_the_orphan_to_edit(
    env: dict[str, str],
) -> None:
    """A review POST whose inline comments are rejected still persists the
    body. Retrying blind duplicates it."""
    _write(env, "REVIEWS_JSON", [_review(5, "2026-01-01T00:00:00Z", body="findings")])

    assert _state(env)["orphan_id"] == 5


def test_a_reply_container_is_never_mistaken_for_an_orphan(
    env: dict[str, str],
) -> None:
    """The body-length test is what separates them, and getting it wrong means
    the recovery PUT overwrites an unrelated reply."""
    _write(env, "REVIEWS_JSON", [_review(5, "2026-01-01T00:00:00Z")])

    assert _state(env)["orphan_id"] is None


def test_a_pre_rewrite_body_is_never_mistaken_for_an_orphan(
    env: dict[str, str],
) -> None:
    """It reports commit_id == head and passes the body test, so without the
    rewrite filter the PUT destroys a published review — overwriting its text
    with this run's findings, over inline comments on code that is gone."""
    _write(env, "REVIEWS_JSON", [_review(5, "2026-01-01T00:00:00Z", body="published")])
    _rewrite_at(env, "2026-01-02T00:00:00Z")

    assert _state(env)["orphan_id"] is None


def test_the_newest_orphan_wins(env: dict[str, str]) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(5, "2026-01-01T00:00:00Z", body="first"),
            _review(6, "2026-01-02T00:00:00Z", body="second"),
        ],
    )

    assert _state(env)["orphan_id"] == 6


# ---------------------------------------------------------------------------
# Shape of the call
# ---------------------------------------------------------------------------


def test_the_head_comes_from_head_ref_oid(env: dict[str, str]) -> None:
    """`--json commits` caps at 100 and returns oldest-first, so on a long PR
    `.commits[-1]` is commit #100. Every head-keyed field then matches nothing
    and goes quiet: the pre-post guard stops firing and a re-run posts a second
    review, the 422 recovery duplicates instead of editing the orphan, and
    `weekly`'s redundant-approval guard lets approvals pile up on one commit."""
    assert _state(env)["head_sha"] == HEAD

    calls = Path(env["GH_CALLS"]).read_text()
    assert "--json headRefOid" in calls, calls


def test_the_repo_is_named_explicitly_on_every_call(env: dict[str, str]) -> None:
    """The script runs from whatever cwd a skill happens to be in — a bare
    `gh pr view` would resolve the wrong repo, or none."""
    _state(env)

    calls = Path(env["GH_CALLS"]).read_text().splitlines()
    lookups = [c for c in calls if c.startswith("pr view") or "repos/" in c]
    assert lookups
    for call in lookups:
        assert "owner/repo" in call, call


def test_review_skill_preserves_the_status_free_queue_contract() -> None:
    """Reading and posting use the same review-state definition."""
    skill = REVIEW_SKILL.read_text()

    assert "repos/$REPO/statuses/$HEAD_SHA" not in skill
    assert "tend-review/<number>" not in skill
    assert "--json headRefOid,state" in skill
    assert '[ "$PR_STATE" != "OPEN" ]' in skill
    assert "scripts/review-preflight.sh <number>" in skill
    assert 'if [ "$EVENT_ACTION" = "ready_for_review" ]; then' in skill
    assert (
        "If `FORCE_FULL_REVIEW` is false and the incremental changes are trivial"
        in skill
    )
    assert f"Open the review body with this exact line: `{DRAFT_REVIEW_LINE}`" in skill
    assert "Post at most one review per run." in skill
    assert "exception to one review per run" not in skill
    assert "STARTED_DRAFT" not in skill
    assert "LIVE_DRAFT" not in skill
