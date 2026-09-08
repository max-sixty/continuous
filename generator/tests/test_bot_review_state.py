"""Tests for bot_review_state.py — which of the bot's reviews anchors the head.

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

from tests import GH_PREAMBLE, fake_bin, tool_path, uv_script

BOT_REVIEW_STATE = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "tend-ci-runner"
    / "scripts"
    / "bot_review_state.py"
)
REVIEW_SKILL = BOT_REVIEW_STATE.parent.parent / "skills" / "review" / "SKILL.md"
RUNNING_IN_CI_SKILL = (
    BOT_REVIEW_STATE.parent.parent / "skills" / "running-in-ci" / "SKILL.md"
)

BOT = "tend-bot"
HEAD = "head000"
DRAFT_REVIEW_MARKER = "<!-- tend:draft-review -->"
READY_REVIEW_MARKER = "<!-- tend:ready-review:101 -->"
REVIEW_COMPLETE_MARKER = "<!-- tend:review-complete -->"
REVIEW_OPERATION_MARKER = (
    "<!-- tend:review-operation:12345678123442348234123456789abc:full -->"
)
OLD = "old0000"

FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$*" in
  "api user"*)          emit '{"login":"'"$BOT_LOGIN"'"}' ;;
  "api graphql"*)       emit "$(cat "$GRAPHQL_JSON")" ;;
  "pr view "*)          emit "$(cat "$PR_HEAD_JSON")" ;;
  *"/pulls/"*"/comments"*) emit "$(cat "$INLINE_JSON")" ;;
  *"/issues/"*"/timeline"*) emit "$(cat "$TIMELINE_JSON")" ;;
  *"/dismissals"*)          emit '{}' ;;
  *"/dispatches --method POST --input -"*) cat > "$DISPATCH_INPUT" ;;
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
        "GRAPHQL_JSON": {},
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
        "CHECKED_HEAD_DIR": str(tmp_path),
        "DISPATCH_INPUT": str(tmp_path / "dispatch-input.json"),
        **paths,
    }


def _state(env: dict[str, str]) -> dict:
    result = subprocess.run(
        uv_script(BOT_REVIEW_STATE, "state", "7"),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        uv_script(BOT_REVIEW_STATE, *args),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


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


def _ready_at(env: dict[str, str], *events: tuple[int, str]) -> None:
    _write(
        env,
        "TIMELINE_JSON",
        [
            {"event": "ready_for_review", "id": event_id, "created_at": at}
            for event_id, at in events
        ],
    )


def _dismissed_at(
    env: dict[str, str],
    review_id: int,
    at: str,
    message: str = "The earlier approval no longer applies.",
) -> None:
    timeline = json.loads(Path(env["TIMELINE_JSON"]).read_text())
    timeline.append(
        {
            "event": "review_dismissed",
            "id": 1000 + review_id,
            "created_at": at,
            "dismissed_review": {
                "review_id": review_id,
                "state": "approved",
                "dismissal_message": message,
            },
        }
    )
    _write(env, "TIMELINE_JSON", timeline)


# ---------------------------------------------------------------------------
# Substantive vs. synthetic reply containers
# ---------------------------------------------------------------------------


def test_a_clean_pr_reports_nothing_anchored(env: dict[str, str]) -> None:
    state = _state(env)

    assert state["head_sha"] == HEAD
    assert state["bot_login"] == BOT
    assert state["last_substantive"] is None
    assert state["last_covered"] is None
    assert state["at_head"] is None
    assert state["fresh_approval_sha"] == ""
    assert state["stale_approval_id"] == ""
    assert state["standing_approval_id"] == ""
    assert state["standing_dismissal"] is None
    assert state["force_pushed_since"] is False
    assert state["latest_ready_for_review"] is None
    assert state["acknowledged_ready_ids"] == []
    assert state["outstanding_ready_for_review"] is None
    assert state["pending_reviews"] == []
    assert state["needs_review"] is True


def test_request_dispatches_only_canonical_outstanding_demand(
    env: dict[str, str],
) -> None:
    requested = _run_cli(env, "request", "7")

    assert requested.returncode == 0, requested.stderr
    assert requested.stdout == "requested: review for PR #7\n"
    assert json.loads(Path(env["DISPATCH_INPUT"]).read_text()) == {
        "event_type": "tend-review",
        "client_payload": {"pr_number": 7},
    }

    Path(env["DISPATCH_INPUT"]).unlink()
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, "2026-01-01T00:00:00Z", body="Complete review.")],
    )
    covered = _run_cli(env, "request", "7")

    assert covered.returncode == 0, covered.stderr
    assert covered.stdout == "skip: PR #7 has no outstanding review demand\n"
    assert not Path(env["DISPATCH_INPUT"]).exists()


def test_feedback_combines_conversation_reviews_and_inline_comments(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "PR_HEAD_JSON",
        {
            "comments": [
                {
                    "author": {"login": "human"},
                    "createdAt": "2026-01-02T00:00:00Z",
                    "body": "question",
                },
                {
                    "author": None,
                    "createdAt": "2026-01-03T00:00:00Z",
                    "body": "deleted account",
                },
            ],
            "reviews": [
                {
                    "author": {"login": BOT},
                    "state": "COMMENTED",
                    "submittedAt": "2026-01-01T00:00:00Z",
                    "body": "finding",
                },
                {"author": {"login": "human"}, "state": "APPROVED", "body": "ok"},
            ],
        },
    )
    _write(
        env,
        "GRAPHQL_JSON",
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "comments": {
                                        "nodes": [
                                            {
                                                "author": {"login": BOT},
                                                "path": "a.py",
                                                "line": 3,
                                                "createdAt": "2026-01-01T01:00:00Z",
                                                "body": "inline",
                                            },
                                            {
                                                "author": {"login": "human"},
                                                "path": "a.py",
                                                "line": 3,
                                                "body": "reply",
                                            },
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        },
    )

    result = _run_cli(env, "feedback", "7")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "previous_reviews": [
            {
                "state": "COMMENTED",
                "submitted_at": "2026-01-01T00:00:00Z",
                "body": "finding",
            }
        ],
        "conversation": [
            {
                "author": "human",
                "created_at": "2026-01-02T00:00:00Z",
                "body": "question",
            },
            {
                "author": "",
                "created_at": "2026-01-03T00:00:00Z",
                "body": "deleted account",
            },
        ],
        "inline_comments": [
            {
                "path": "a.py",
                "line": 3,
                "created_at": "2026-01-01T01:00:00Z",
                "body": "inline",
            }
        ],
        "pending_inline_comments": [],
        "pending_reviews": [],
    }


def test_feedback_separates_comments_from_private_pending_reviews(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                7,
                None,
                state="PENDING",
                body=f"Unsubmitted narrative.\n\n{REVIEW_OPERATION_MARKER}",
            )
        ],
    )
    _write(env, "PR_HEAD_JSON", {"comments": [], "reviews": []})
    _write(
        env,
        "INLINE_JSON",
        [
            {
                "path": "incomplete.py",
                "line": 3,
                "created_at": "2026-01-01T01:00:00Z",
                "body": "partial finding",
            }
        ],
    )
    _write(
        env,
        "GRAPHQL_JSON",
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "comments": {
                                        "nodes": [
                                            {
                                                "author": {"login": BOT},
                                                "path": "incomplete.py",
                                                "line": 3,
                                                "createdAt": "2026-01-01T01:00:00Z",
                                                "body": "partial finding",
                                                "pullRequestReview": {
                                                    "author": {"login": BOT},
                                                    "body": REVIEW_OPERATION_MARKER,
                                                    "state": "PENDING",
                                                    "fullDatabaseId": 7,
                                                },
                                            },
                                            {
                                                "author": {"login": BOT},
                                                "path": "published.py",
                                                "line": 4,
                                                "createdAt": "2026-01-02T01:00:00Z",
                                                "body": "published finding",
                                                "pullRequestReview": {
                                                    "author": {"login": BOT},
                                                    "body": "Final review.",
                                                    "state": "COMMENTED",
                                                    "fullDatabaseId": 8,
                                                },
                                            },
                                            {
                                                "author": {"login": BOT},
                                                "path": "human-review.py",
                                                "line": 5,
                                                "createdAt": "2026-01-03T01:00:00Z",
                                                "body": "bot reply",
                                                "pullRequestReview": {
                                                    "author": {"login": "human"},
                                                    "body": REVIEW_OPERATION_MARKER,
                                                    "state": "COMMENTED",
                                                    "fullDatabaseId": 9,
                                                },
                                            },
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        },
    )

    result = _run_cli(env, "feedback", "7")

    assert result.returncode == 0, result.stderr
    feedback = json.loads(result.stdout)
    assert [item["body"] for item in feedback["inline_comments"]] == [
        "published finding",
        "bot reply",
    ]
    assert feedback["pending_inline_comments"] == [
        {
            "review_id": 7,
            "path": "incomplete.py",
            "line": 3,
            "created_at": "2026-01-01T01:00:00Z",
            "body": "partial finding",
        }
    ]
    assert feedback["pending_reviews"] == [
        {
            "review_id": 7,
            "sha": HEAD,
            "review_mode": "full",
            "ready_review_ids": [],
            "body": "Unsubmitted narrative.",
        }
    ]
    assert (
        "pullRequestReview { author { login } body state fullDatabaseId }"
        in Path(env["GH_CALLS"]).read_text()
    )


def test_threads_filters_to_unresolved_threads_started_by_the_bot(
    env: dict[str, str],
) -> None:
    def thread(thread_id: str, *, bot: str = BOT, resolved: bool = False) -> dict:
        return {
            "id": thread_id,
            "isResolved": resolved,
            "comments": {
                "nodes": [
                    {
                        "author": {"login": bot},
                        "path": "a.py",
                        "line": 3,
                        "body": "finding",
                    }
                ]
            },
        }

    _write(
        env,
        "GRAPHQL_JSON",
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                thread("keep"),
                                thread("resolved", resolved=True),
                                thread("human", bot="human"),
                            ]
                        }
                    }
                }
            }
        },
    )

    result = _run_cli(env, "threads", "7")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"id": "keep", "path": "a.py", "line": 3, "body": "finding"}
    ]


def test_resolve_thread_uses_the_graphql_mutation(env: dict[str, str]) -> None:
    result = _run_cli(env, "resolve-thread", "THREAD_kwDO")

    assert result.returncode == 0, result.stderr
    calls = Path(env["GH_CALLS"]).read_text()
    assert "api graphql" in calls
    assert "threadId=THREAD_kwDO" in calls
    assert "resolveReviewThread" in calls


def test_an_unsubmitted_review_anchors_nothing(env: dict[str, str]) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, None, body="draft findings", state="PENDING")],
    )

    state = _state(env)

    assert state["last_substantive"] is None
    assert state["at_head"] is None


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


def test_a_dismissed_review_no_longer_covers_even_when_its_body_remains(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                1,
                "2026-01-01T00:00:00Z",
                body="Approval context that GitHub retains after dismissal.",
                state="DISMISSED",
            )
        ],
    )
    _dismissed_at(env, 1, "2026-01-02T00:00:00Z", "Superseded by another PR.")

    state = _state(env)

    assert state["last_substantive"] is None
    assert state["last_covered"] is None
    assert state["at_head"] is None
    assert state["needs_review"] is True
    assert state["standing_dismissal"] == {
        "event_id": 1001,
        "review_id": 1,
        "sha": HEAD,
        "review_at": "2026-01-01T00:00:00Z",
        "at": "2026-01-02T00:00:00Z",
        "message": "Superseded by another PR.",
        "prior_state": "approved",
        "dismissal_commit_id": "",
    }


@pytest.mark.parametrize(
    "earlier_body",
    ["Draft finding.", REVIEW_COMPLETE_MARKER],
    ids=["comment", "silent-completion"],
)
def test_a_later_dismissal_invalidates_earlier_coverage_on_the_same_head(
    env: dict[str, str], earlier_body: str
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(1, "2026-01-01T00:00:00Z", body=earlier_body),
            _review(
                2,
                "2026-01-02T00:00:00Z",
                body=READY_REVIEW_MARKER,
                state="DISMISSED",
            ),
        ],
    )
    _dismissed_at(env, 2, "2026-01-03T00:00:00Z")

    state = _state(env)

    assert state["last_covered"] is None
    assert state["at_head"] is None
    assert state["needs_review"] is True


def test_silent_completion_does_not_erase_standing_dismissal_context(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(1, "2026-01-01T00:00:00Z", state="DISMISSED"),
            _review(2, "2026-01-03T00:00:00Z", body=REVIEW_COMPLETE_MARKER),
        ],
    )
    _dismissed_at(env, 1, "2026-01-02T00:00:00Z", "Approach was rejected.")

    state = _state(env)

    assert state["at_head"]["id"] == 2
    assert state["needs_review"] is False
    assert state["standing_dismissal"]["message"] == "Approach was rejected."


def test_a_later_bot_approval_supersedes_standing_dismissal_context(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(1, "2026-01-01T00:00:00Z", state="DISMISSED"),
            _review(2, "2026-01-03T00:00:00Z", state="APPROVED"),
        ],
    )
    _dismissed_at(env, 1, "2026-01-02T00:00:00Z")

    assert _state(env)["standing_dismissal"] is None


def test_native_dismissal_accepts_githubs_documented_string_review_id(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, "2026-01-01T00:00:00Z", state="DISMISSED")],
    )
    _dismissed_at(env, 1, "2026-01-02T00:00:00Z")
    timeline = json.loads(Path(env["TIMELINE_JSON"]).read_text())
    timeline[0]["dismissed_review"]["review_id"] = "1"
    _write(env, "TIMELINE_JSON", timeline)

    assert _state(env)["standing_dismissal"]["review_id"] == 1


def test_same_second_approval_cannot_erase_dismissal_context(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(1, "2026-01-01T00:00:00Z", state="DISMISSED"),
            _review(2, "2026-01-02T00:00:00Z", state="APPROVED"),
        ],
    )
    _dismissed_at(env, 1, "2026-01-02T00:00:00Z")

    state = _state(env)

    assert state["standing_dismissal"] is not None
    assert state["at_head"] is None
    assert state["needs_review"] is True


def test_approval_before_a_later_dismissal_cannot_suppress_recovery(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(1, "2026-01-01T00:00:00Z", state="DISMISSED"),
            _review(2, "2026-01-02T00:00:00Z", state="APPROVED"),
        ],
    )
    _dismissed_at(env, 1, "2026-01-03T00:00:00Z", "A blocker still applies.")

    state = _state(env)

    assert state["standing_dismissal"] is not None
    assert state["at_head"] is None
    assert state["needs_review"] is True


def test_same_second_review_submissions_leave_coverage_open_for_retry(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(1, "2026-01-01T00:00:00Z", state="DISMISSED"),
            _review(2, "2026-01-01T00:00:00Z", state="APPROVED"),
        ],
    )
    _dismissed_at(env, 1, "2026-01-02T00:00:00Z")

    state = _state(env)

    assert state["standing_dismissal"] is not None
    assert state["at_head"] is None
    assert state["needs_review"] is True


def test_findings_posted_after_approval_survive_its_later_dismissal(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(1, "2026-01-01T00:00:00Z", state="DISMISSED"),
            _review(2, "2026-01-02T00:00:00Z", body="Blocking finding."),
        ],
    )
    _dismissed_at(env, 1, "2026-01-03T00:00:00Z")

    state = _state(env)

    assert state["at_head"]["id"] == 2
    assert state["needs_review"] is False
    assert state["standing_dismissal"] is not None


def test_findings_cannot_hide_an_approval_that_conflicts_with_a_dismissal(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(1, "2026-01-01T00:00:00Z", state="DISMISSED"),
            _review(2, "2026-01-02T00:00:00Z", body="Blocking finding."),
            _review(3, "2026-01-03T00:00:00Z", state="APPROVED"),
        ],
    )
    _dismissed_at(env, 1, "2026-01-04T00:00:00Z", "The finding still applies.")

    state = _state(env)

    assert state["at_head"]["id"] == 2
    assert state["standing_approval_id"] == 3
    assert state["standing_dismissal"] is not None
    assert state["needs_review"] is True


def test_dismissed_review_without_native_timeline_metadata_fails_closed(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, "2026-01-01T00:00:00Z", state="DISMISSED")],
    )

    result = _run_cli(env, "state", "7")

    assert result.returncode != 0
    assert "dismissed bot review is missing timeline metadata" in result.stderr


def test_at_head_uses_native_review_fields_not_prose_identity(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, "2026-01-01T00:00:00Z", body="One landing concern.")],
    )

    assert _state(env)["at_head"] == {
        "id": 1,
        "state": "COMMENTED",
        "at": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Ready-for-review generations and private review metadata
# ---------------------------------------------------------------------------


def test_only_the_latest_exact_ready_generation_can_be_outstanding(
    env: dict[str, str],
) -> None:
    """Events are state generations, not queued work: an acknowledgment of
    the newest one semantically supersedes every older generation."""
    _ready_at(
        env,
        (99, "2026-01-02T00:00:00Z"),
        (101, "2026-01-03T00:00:00Z"),
        (100, "2026-01-03T00:00:00Z"),
    )
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                1,
                "2026-01-04T00:00:00Z",
                body="Full pass.\n\n<!-- tend:ready-review:99 -->",
            )
        ],
    )

    state = _state(env)

    assert state["latest_ready_for_review"] == {
        "id": 101,
        "at": "2026-01-03T00:00:00Z",
    }
    assert state["acknowledged_ready_ids"] == [99]
    assert state["outstanding_ready_for_review"] == state["latest_ready_for_review"]

    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                2,
                "2026-01-05T00:00:00Z",
                state="APPROVED",
                body=READY_REVIEW_MARKER,
            )
        ],
    )

    state = _state(env)

    assert state["acknowledged_ready_ids"] == [101]
    assert state["outstanding_ready_for_review"] is None


def test_submission_time_does_not_acknowledge_a_ready_generation(
    env: dict[str, str],
) -> None:
    """A session can start before ready and post afterward without doing the
    requested full pass; only the exact event marker proves observation."""
    _ready_at(env, (101, "2026-01-02T00:00:00Z"))
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, "2026-01-03T00:00:00Z", state="APPROVED")],
    )

    state = _state(env)

    assert state["at_head"]["id"] == 1
    assert state["outstanding_ready_for_review"]["id"] == 101


def test_a_pending_review_cannot_acknowledge_readiness(env: dict[str, str]) -> None:
    _ready_at(env, (101, "2026-01-02T00:00:00Z"))
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                1,
                None,
                state="PENDING",
                body=(
                    f"Work-in-progress finding.\n{REVIEW_OPERATION_MARKER}\n"
                    f"{READY_REVIEW_MARKER}"
                ),
            )
        ],
    )

    state = _state(env)

    assert state["at_head"] is None
    assert state["acknowledged_ready_ids"] == []
    assert state["outstanding_ready_for_review"]["id"] == 101


def test_only_a_finalized_bot_review_acknowledges_readiness(
    env: dict[str, str],
) -> None:
    _ready_at(env, (101, "2026-01-02T00:00:00Z"))
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                1,
                "2026-01-03T00:00:00Z",
                author="human",
                state="APPROVED",
                body=READY_REVIEW_MARKER,
            ),
            _review(2, None, state="PENDING", body=READY_REVIEW_MARKER),
        ],
    )

    state = _state(env)

    assert state["acknowledged_ready_ids"] == []
    assert state["at_head"] is None
    assert state["outstanding_ready_for_review"]["id"] == 101


def test_a_readiness_marker_alone_acknowledges_without_becoming_coverage(
    env: dict[str, str],
) -> None:
    _ready_at(env, (101, "2026-01-02T00:00:00Z"))
    _write(
        env,
        "REVIEWS_JSON",
        [_review(3, "2026-01-03T00:00:00Z", body=READY_REVIEW_MARKER)],
    )

    state = _state(env)

    assert state["acknowledged_ready_ids"] == [101]
    assert state["outstanding_ready_for_review"] is None
    assert state["last_substantive"] is None
    assert state["at_head"] is None


def test_a_completion_marker_is_durable_coverage_without_public_feedback(
    env: dict[str, str],
) -> None:
    _ready_at(env, (101, "2026-01-02T00:00:00Z"))
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                4,
                "2026-01-03T00:00:00Z",
                body=f"{REVIEW_COMPLETE_MARKER}\n\n{READY_REVIEW_MARKER}",
            )
        ],
    )

    state = _state(env)

    assert state["last_substantive"] is None
    assert state["last_covered"]["id"] == 4
    assert state["at_head"]["id"] == 4
    assert state["acknowledged_ready_ids"] == [101]
    assert state["outstanding_ready_for_review"] is None
    assert state["needs_review"] is False


def test_a_finalized_inline_review_acknowledges_readiness(
    env: dict[str, str],
) -> None:
    _ready_at(env, (101, "2026-01-02T00:00:00Z"))
    _write(
        env,
        "REVIEWS_JSON",
        [_review(5, "2026-01-03T00:00:00Z", body=READY_REVIEW_MARKER)],
    )
    _write(env, "INLINE_JSON", [{"pull_request_review_id": 5}])

    state = _state(env)

    assert state["at_head"]["id"] == 5
    assert state["acknowledged_ready_ids"] == [101]
    assert state["outstanding_ready_for_review"] is None


def test_pending_inline_review_is_recoverable_but_never_coverage(
    env: dict[str, str],
) -> None:
    _ready_at(env, (101, "2026-01-02T00:00:00Z"))
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                7,
                None,
                state="PENDING",
                body=f"{REVIEW_OPERATION_MARKER}\n{READY_REVIEW_MARKER}",
            )
        ],
    )
    _write(env, "INLINE_JSON", [{"pull_request_review_id": 7}])

    state = _state(env)

    assert state["pending_reviews"] == [
        {
            "id": 7,
            "sha": HEAD,
            "operation_id": "12345678123442348234123456789abc",
            "draft_mode": False,
            "ready_review_ids": [101],
            "body": "",
        }
    ]
    assert state["last_substantive"] is None
    assert state["at_head"] is None
    assert state["acknowledged_ready_ids"] == []


def test_pending_review_on_another_head_remains_visible_for_cleanup(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                7,
                None,
                state="PENDING",
                body=REVIEW_OPERATION_MARKER,
                sha=OLD,
            )
        ],
    )
    _rewrite_at(env, "2026-01-02T00:00:00Z")

    assert _state(env)["pending_reviews"] == [
        {
            "id": 7,
            "sha": OLD,
            "operation_id": "12345678123442348234123456789abc",
            "draft_mode": False,
            "ready_review_ids": [],
            "body": "",
        }
    ]


def test_only_bot_pending_reviews_with_operation_ids_are_recoverable(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                1,
                None,
                author="human",
                state="PENDING",
                body=REVIEW_OPERATION_MARKER,
            ),
            _review(2, None, state="PENDING", body="unowned pending review"),
            _review(
                3,
                "2026-01-01T00:00:00Z",
                state="APPROVED",
                body=REVIEW_OPERATION_MARKER,
            ),
        ],
    )

    assert _state(env)["pending_reviews"] == []


def test_review_metadata_is_not_public_feedback(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "PR_HEAD_JSON",
        {
            "comments": [],
            "reviews": [
                {
                    "author": {"login": BOT},
                    "state": "PENDING",
                    "submittedAt": None,
                    "body": REVIEW_OPERATION_MARKER,
                },
                {
                    "author": {"login": BOT},
                    "state": "COMMENTED",
                    "submittedAt": "2026-01-02T00:00:00Z",
                    "body": (
                        f"Finding.\n\n{REVIEW_COMPLETE_MARKER}\n"
                        f"{READY_REVIEW_MARKER}\n{DRAFT_REVIEW_MARKER}"
                    ),
                },
            ],
        },
    )
    _write(
        env,
        "GRAPHQL_JSON",
        {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}},
    )

    result = _run_cli(env, "feedback", "7")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["previous_reviews"] == [
        {
            "state": "COMMENTED",
            "submitted_at": "2026-01-02T00:00:00Z",
            "body": "Finding.",
        }
    ]


def test_malformed_reserved_markers_cannot_become_public_metadata(
    env: dict[str, str],
) -> None:
    """The parser accepts only canonical IDs, while public-body sanitization
    reserves the whole namespace rather than leaking malformed lookalikes."""
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                7,
                "2026-01-03T00:00:00Z",
                body=(
                    "Finding.\n\n<!-- tend:ready-review:not-an-id -->\n"
                    "<!-- tend:review-operation:NOT-A-UUID -->"
                ),
            )
        ],
    )

    state = _state(env)

    assert state["at_head"]["id"] == 7
    assert state["acknowledged_ready_ids"] == []


def test_metadata_sanitization_cannot_synthesize_a_reserved_marker(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(
                7,
                "2026-01-03T00:00:00Z",
                body="<!-- tend:ready-<!-- tend:ready-review:999 -->review:42 -->",
            )
        ],
    )

    state = _state(env)

    assert state["acknowledged_ready_ids"] == [999]
    assert state["pending_reviews"] == []
    assert state["pending_reviews"] == []


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


def test_a_deleted_review_author_is_not_ours(env: dict[str, str]) -> None:
    review = _review(1, "2026-01-01T00:00:00Z", state="APPROVED")
    review["user"] = None
    _write(env, "REVIEWS_JSON", [review])

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


def test_a_rewrite_tied_with_the_review_timestamp_wins_conservatively(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(1, "2026-01-01T00:00:00Z", body="findings")],
    )
    _rewrite_at(env, "2026-01-01T00:00:00Z")

    state = _state(env)

    assert state["force_pushed_since"] is True
    assert state["at_head"] is None


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
    _dismissed_at(env, 3, "2026-01-01T12:00:00Z")

    assert _state(env)["stale_approval_id"] == ""


def test_an_approval_stands_through_ordinary_pushes_and_comment_reviews(
    env: dict[str, str],
) -> None:
    """GitHub never lets a later COMMENTED supersede an APPROVED, so a PR that
    takes ordinary pushes onto a bot approval and then draws findings-bearing
    re-reviews still merges reading APPROVED. No rewrite happened, so
    `stale_approval_id` — which is keyed on one — cannot see it."""
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(3, "2026-01-01T00:00:00Z", state="APPROVED", sha=OLD),
            _review(4, "2026-01-02T00:00:00Z", body="findings", sha=OLD),
            _review(5, "2026-01-03T00:00:00Z", body="more findings"),
        ],
    )

    state = _state(env)

    assert state["standing_approval_id"] == 3
    assert state["stale_approval_id"] == "", "no rewrite happened"


def test_a_dismissed_approval_is_not_standing(env: dict[str, str]) -> None:
    """Dismissing rewrites the record's state, so the next run's findings
    review does not dismiss it a second time."""
    _write(env, "REVIEWS_JSON", [_review(3, "2026-01-01T00:00:00Z", state="DISMISSED")])
    _dismissed_at(env, 3, "2026-01-01T12:00:00Z")

    assert _state(env)["standing_approval_id"] == ""


def test_the_newest_approval_is_the_standing_one(env: dict[str, str]) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(3, "2026-01-01T00:00:00Z", state="APPROVED", sha=OLD),
            _review(4, "2026-01-02T00:00:00Z", state="APPROVED"),
        ],
    )

    assert _state(env)["standing_approval_id"] == 4


def test_a_later_changes_requested_supersedes_the_approval(
    env: dict[str, str],
) -> None:
    """CHANGES_REQUESTED does set the PR's review decision, so the approval it
    replaced is no longer what a findings review has to clear."""
    _write(
        env,
        "REVIEWS_JSON",
        [
            _review(3, "2026-01-01T00:00:00Z", state="APPROVED", sha=OLD),
            _review(4, "2026-01-02T00:00:00Z", state="CHANGES_REQUESTED"),
        ],
    )

    assert _state(env)["standing_approval_id"] == ""


def test_dismiss_command_clears_only_the_standing_approval(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(3, "2026-01-01T00:00:00Z", state="APPROVED")],
    )

    result = _run_cli(env, "dismiss", "7", "Superseded by findings")

    assert result.returncode == 0, result.stderr
    calls = Path(env["GH_CALLS"]).read_text()
    assert "/reviews/3/dismissals -X PUT -f message=Superseded by findings" in calls

    Path(env["GH_CALLS"]).write_text("")
    _write(
        env,
        "REVIEWS_JSON",
        [_review(4, "2026-01-02T00:00:00Z", state="CHANGES_REQUESTED")],
    )
    result = _run_cli(env, "dismiss", "7", "Superseded by findings")
    assert result.returncode == 0, result.stderr
    assert "/dismissals" not in Path(env["GH_CALLS"]).read_text()


def test_dismiss_stale_command_targets_only_the_pre_rewrite_approval(
    env: dict[str, str],
) -> None:
    _write(
        env,
        "REVIEWS_JSON",
        [_review(3, "2026-01-01T00:00:00Z", state="APPROVED")],
    )
    _rewrite_at(env, "2026-01-02T00:00:00Z")

    result = _run_cli(env, "dismiss-stale", "7", "Rebased")

    assert result.returncode == 0, result.stderr
    calls = Path(env["GH_CALLS"]).read_text()
    assert "/reviews/3/dismissals -X PUT -f message=Rebased" in calls


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


def test_an_approval_whose_commit_was_deleted_has_no_fresh_sha(
    env: dict[str, str],
) -> None:
    review = _review(3, "2026-01-01T00:00:00Z", state="APPROVED")
    review["commit_id"] = None
    _write(env, "REVIEWS_JSON", [review])

    assert _state(env)["fresh_approval_sha"] == ""


# ---------------------------------------------------------------------------
# Shape of the call
# ---------------------------------------------------------------------------


def test_the_head_comes_from_head_ref_oid(env: dict[str, str]) -> None:
    """`--json commits` caps at 100 and returns oldest-first, so on a long PR
    `.commits[-1]` is commit #100. Every head-keyed field then matches nothing
    and goes quiet: the pre-post guard stops firing and a re-run posts a second
    review, incomplete recovery cannot match its review, and `weekly`'s
    redundant-approval guard lets approvals pile up on one commit."""
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
    assert 'review_preflight.py" start <number>' in skill
    assert (
        'if state != "OPEN"'
        in BOT_REVIEW_STATE.with_name("review_preflight.py").read_text()
    )
    assert "force_full_review" in skill
    assert (
        "If `force_full_review` is false and the incremental changes are trivial"
        in skill
    )
    assert "submit command uses the captured draft mode itself" in skill
    assert "Open the review body with this exact line" not in skill
    assert "Post at most one review per run." in skill
    assert "exception to one review per run" not in skill
    assert "STARTED_DRAFT" not in skill
    assert "LIVE_DRAFT" not in skill


def test_review_skill_dismisses_a_standing_approval_when_it_posts_findings() -> None:
    """The rule sits at the posting site, not in one push-shape's branch: the
    force-push and ordinary-push paths fail identically, and a rule stated for
    only one of them merges findings under a bot APPROVED."""
    skill = REVIEW_SKILL.read_text()

    assert "bot_review_state.py" in skill
    assert "dismiss <number>" in skill
    assert "A findings review never supersedes a standing approval" in skill
    # The force-push branch defers to that one rule rather than restating it.
    assert "last_substantive.state, .last_substantive.id" not in skill


def test_review_skill_defines_every_id_its_dismissal_recipes_use() -> None:
    """Step 7's CI-failure dismissal read `$REVIEW_ID`, which step 1 was the
    only site to name. With that sentence gone the path collapsed to
    `reviews//dismissals`, leaving an approval standing over a red check."""
    skill = REVIEW_SKILL.read_text()

    assert "$REVIEW_ID" not in skill
    # Every dismissal recipe invokes the same command, so there is one mechanism.
    assert skill.count("dismiss <number>") == 3
    assert "reviews/$STANDING/dismissals" not in skill


def test_review_skill_spares_the_approval_when_the_comment_withholds_nothing() -> None:
    """Step 1's unanswered-question exception posts a COMMENT at a head the
    approval already covers. A trigger keyed on any COMMENT dismisses there,
    withdrawing a verdict the code still earns and leaving the PR with none —
    the next run hits the already-reviewed shortcut and posts nothing."""
    skill = REVIEW_SKILL.read_text()

    assert "posts a COMMENT that withholds the verdict" in skill
    assert "A COMMENT that withholds nothing does not qualify" in skill
    assert "whenever this round posts a COMMENT rather than an approval" not in skill


def test_a_dismissal_path_exists_for_an_invalidation_that_is_not_an_event() -> None:
    """Every dismissal site in `review` and `weekly` is keyed on something that
    happened *on* the approved PR — a review round, a rewrite, a red check. An
    approval superseded by a *different* PR merging reaches none of them, so it
    stands until a human clears it. The generic rule lives in `running-in-ci`,
    which every skill that can reach that conclusion loads, and it fires on the
    conclusion rather than on a post — the dedup rules routinely (and rightly)
    suppress the comment that would otherwise carry it."""
    skill = RUNNING_IN_CI_SKILL.read_text()

    assert "bot_review_state.py" in skill
    assert "dismiss <number>" in skill
    assert "reviews/$STANDING/dismissals" not in skill
    # Not keyed on this session posting anything.
    assert "whether or not this session posts" in skill
