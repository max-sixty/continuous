"""Tests for plugins/tend-ci-runner/scripts/review_runs_corrections.py.

Step 4 of review-runs decides whether the tracking issue records "no
maintainer corrections", which later runs read as ground truth under Gate 1.
Every filter here fails open when its input is empty — an unset anchor
compares less than every timestamp, `--author ""` applies no author filter —
so an empty result is only trustworthy if the inputs were checked. These pin
that, plus the three window edges the query has to get right: the bot's own
output excluded, the empty-bodied review container GitHub synthesises for an
inline reply excluded, and a review on an in-window PR that was itself
submitted before the window excluded.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests import GH_PREAMBLE, fake_bin, tool_path, uv_script

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "tend-ci-runner"
    / "scripts"
    / "review_runs_corrections.py"
)

BOT = "tend-bot"
HUMAN = "maintainer"
SINCE = "2026-01-02T00:00:00Z"
IN_WINDOW = "2026-01-02T12:00:00Z"
BEFORE = "2026-01-01T12:00:00Z"

FAKE_GH = (
    GH_PREAMBLE
    + r"""
emit_json() {
  if [ "${CLICOLOR_FORCE:-}" = "1" ]; then
    printf '\033[36m%s\033[0m\n' "$1"
  else
    emit "$1"
  fi
}

case "$*" in
  "api user"*)             emit '{"login":"'"$BOT_LOGIN"'"}' ;;
  # The reviews candidate list is the only `pr list` carrying --search.
  "pr list"*--search*)
    [ -n "${SEARCH_FAILS:-}" ] && { echo "API rate limit exceeded" >&2; exit 1; }
    emit "$(cat "$CANDIDATES_JSON")" ;;
  "pr list"*)              emit_json "$(cat "$BOT_PRS_JSON")" ;;
  *"/issues/comments"*)
    [ -n "${ISSUE_COMMENTS_FAILS:-}" ] && { echo "issues unavailable" >&2; exit 17; }
    emit "$(cat "$ISSUE_COMMENTS_JSON")" ;;
  *"/pulls/comments"*)     emit "$(cat "$PR_COMMENTS_JSON")" ;;
  *"/pulls/"*"/reviews"*)
    if [ -n "${REVIEW_FAIL_PR:-}" ] && [[ "$*" == *"/pulls/$REVIEW_FAIL_PR/reviews"* ]]; then
      echo "reviews unavailable" >&2
      exit 18
    fi
    emit "$(cat "$REVIEWS_JSON")" ;;
  *) exit 1 ;;
esac
"""
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Fake `gh` plus fixtures for a repo with nothing in the window."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH)
    fixtures = {
        "BOT_PRS_JSON": [],
        "CANDIDATES_JSON": [],
        "ISSUE_COMMENTS_JSON": [],
        "PR_COMMENTS_JSON": [],
        "REVIEWS_JSON": [],
    }
    paths = {}
    for key, value in fixtures.items():
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


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        uv_script(SCRIPT, *args), env=env, capture_output=True, text=True, check=False
    )


def _collect(env: dict[str, str], since: str = SINCE) -> dict:
    result = _run(env, since)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _write(env: dict[str, str], key: str, value: object) -> None:
    Path(env[key]).write_text(json.dumps(value))


def _comment(at: str, *, author: str = HUMAN, updated: str | None = None) -> dict:
    return {
        "user": {"login": author},
        "created_at": at,
        "updated_at": updated or at,
        "html_url": f"https://github.com/owner/repo/issues/1#c{at}",
        "body": "that is not what the code does",
    }


def _review(at: str, *, author: str = HUMAN, body: str = "wrong", **kw) -> dict:
    return {
        "user": {"login": author},
        "submitted_at": at,
        "state": kw.get("state", "CHANGES_REQUESTED"),
        "html_url": f"https://github.com/owner/repo/pull/1#r{at}",
        "body": body,
    }


# ---------------------------------------------------------------------------
# Fail loudly rather than reporting an empty window
# ---------------------------------------------------------------------------


def test_a_missing_anchor_is_an_error_not_an_empty_window(env: dict[str, str]) -> None:
    """Unset, `$SINCE` compares less than every timestamp, so the window would
    silently widen to everything — and a caller that dropped the anchor gets no
    signal it did."""
    result = _run(env)

    assert result.returncode == 2
    assert "usage" in result.stderr
    assert not Path(env["GH_CALLS"]).exists()


def test_an_unresolvable_bot_login_is_an_error(env: dict[str, str]) -> None:
    """`--author ""` applies no author filter and `.user.login != ""` is true
    for everyone, so an empty login reports the bot's own output as maintainer
    corrections."""
    env["BOT_LOGIN"] = ""

    result = _run(env, SINCE)

    assert result.returncode == 2
    assert "bot login" in result.stderr


def test_a_failed_candidate_search_aborts_rather_than_reporting_no_reviews(
    env: dict[str, str],
) -> None:
    """The candidate query runs on the search API's 30 req/min budget, so it is
    the call most likely to fail — and inline in the `for` list its failure is
    invisible to `set -e`, leaving `reviews: []` to read as an all-clear."""
    env["SEARCH_FAILS"] = "1"

    result = _run(env, SINCE)

    assert result.returncode != 0
    assert result.stdout == ""


def test_a_failed_comment_endpoint_aborts_rather_than_reporting_the_other_half(
    env: dict[str, str],
) -> None:
    _write(env, "PR_COMMENTS_JSON", [_comment(IN_WINDOW)])
    env["ISSUE_COMMENTS_FAILS"] = "1"

    result = _run(env, SINCE)

    assert result.returncode != 0
    assert result.stdout == ""


def test_a_failed_review_fetch_aborts_rather_than_dropping_that_prs_reviews(
    env: dict[str, str],
) -> None:
    _write(env, "CANDIDATES_JSON", [{"number": 1}, {"number": 2}])
    _write(env, "REVIEWS_JSON", [_review(IN_WINDOW)])
    env["REVIEW_FAIL_PR"] = "1"

    result = _run(env, SINCE)

    assert result.returncode != 0
    assert result.stdout == ""


def test_since_is_encoded_when_used_as_an_api_query(env: dict[str, str]) -> None:
    since = "2026-01-02T01:00:00+01:00"

    _collect(env, since)

    calls = Path(env["GH_CALLS"]).read_text()
    assert "since=2026-01-02T01%3A00%3A00%2B01%3A00" in calls


def test_a_quiet_window_reports_empty_lists(env: dict[str, str]) -> None:
    out = _collect(env)

    assert out == {
        "since": SINCE,
        "bot": BOT,
        "dispositions": [],
        "comments": [],
        "reviews": [],
    }


def test_forced_cli_color_cannot_corrupt_json(env: dict[str, str]) -> None:
    env["CLICOLOR_FORCE"] = "1"

    assert _collect(env)["dispositions"] == []


# ---------------------------------------------------------------------------
# Dispositions
# ---------------------------------------------------------------------------


def test_dispositions_are_windowed_and_open_prs_excluded(env: dict[str, str]) -> None:
    """`closedAt` is null on an open PR, and null compares less than the
    anchor, so an open PR is not a disposition."""
    _write(
        env,
        "BOT_PRS_JSON",
        [
            {
                "number": 1,
                "title": "merged in",
                "state": "MERGED",
                "closedAt": IN_WINDOW,
            },
            {
                "number": 2,
                "title": "closed before",
                "state": "CLOSED",
                "closedAt": BEFORE,
            },
            {"number": 3, "title": "still open", "state": "OPEN", "closedAt": None},
        ],
    )

    out = _collect(env)

    assert [d["number"] for d in out["dispositions"]] == [1]


# ---------------------------------------------------------------------------
# Thread comments
# ---------------------------------------------------------------------------


def test_bot_comments_are_excluded_from_both_endpoints(env: dict[str, str]) -> None:
    _write(env, "ISSUE_COMMENTS_JSON", [_comment(IN_WINDOW, author=BOT)])
    _write(env, "PR_COMMENTS_JSON", [_comment(IN_WINDOW, author=BOT)])

    assert _collect(env)["comments"] == []


def test_a_deleted_comment_author_is_still_a_human_correction(
    env: dict[str, str],
) -> None:
    comment = _comment(IN_WINDOW)
    comment["user"] = None
    _write(env, "ISSUE_COMMENTS_JSON", [comment])

    (row,) = _collect(env)["comments"]

    assert row["body"] == "that is not what the code does"


def test_both_comment_endpoints_are_read_and_paginated(env: dict[str, str]) -> None:
    """The response is ascending by `created_at`, so a single unpaginated page
    drops the newest comments — the ones a fresh correction sits in."""
    _write(env, "ISSUE_COMMENTS_JSON", [_comment(IN_WINDOW)])
    _write(env, "PR_COMMENTS_JSON", [_comment(IN_WINDOW)])

    assert len(_collect(env)["comments"]) == 2

    calls = Path(env["GH_CALLS"]).read_text().splitlines()
    for endpoint in ("issues", "pulls"):
        assert any(
            "--paginate" in c and f"repos/owner/repo/{endpoint}/comments" in c
            for c in calls
        ), endpoint


def test_an_edited_older_comment_reports_both_timestamps(env: dict[str, str]) -> None:
    """`since` filters on `updated_at`, so a comment created before the window
    and edited inside it is a real hit — the projection has to show why."""
    _write(env, "ISSUE_COMMENTS_JSON", [_comment(BEFORE, updated=IN_WINDOW)])

    (row,) = _collect(env)["comments"]

    assert row["created"] == BEFORE
    assert row["updated"] == IN_WINDOW


# ---------------------------------------------------------------------------
# Review bodies — the signal neither comment endpoint returns
# ---------------------------------------------------------------------------


def test_a_human_review_body_is_collected(env: dict[str, str]) -> None:
    _write(env, "CANDIDATES_JSON", [{"number": 7}])
    _write(env, "REVIEWS_JSON", [_review(IN_WINDOW)])

    (row,) = _collect(env)["reviews"]

    assert row["state"] == "CHANGES_REQUESTED"
    assert row["at"] == IN_WINDOW


def test_a_failed_review_page_aborts_even_when_a_later_pr_succeeds(
    env: dict[str, str],
) -> None:
    _write(env, "CANDIDATES_JSON", [{"number": 7}, {"number": 8}])
    env["REVIEW_FAIL_PR"] = "7"

    result = _run(env, SINCE)

    assert result.returncode != 0
    assert result.stdout == ""


def test_an_empty_bodied_review_container_is_not_a_correction(
    env: dict[str, str],
) -> None:
    """GitHub wraps a standalone inline reply in an empty-bodied review, so
    without the body check every thread where anyone replied inline reports a
    correction."""
    _write(env, "CANDIDATES_JSON", [{"number": 7}])
    _write(env, "REVIEWS_JSON", [_review(IN_WINDOW, body="", state="COMMENTED")])

    assert _collect(env)["reviews"] == []


def test_an_out_of_window_review_on_an_in_window_pr_is_excluded(
    env: dict[str, str],
) -> None:
    """`updated:` windows the PR, not the review: a PR touched today surfaces
    its whole review history, so `submitted_at` is what makes the window
    exact."""
    _write(env, "CANDIDATES_JSON", [{"number": 7}])
    _write(env, "REVIEWS_JSON", [_review(BEFORE), _review(IN_WINDOW)])

    assert [r["at"] for r in _collect(env)["reviews"]] == [IN_WINDOW]


def test_bot_reviews_are_excluded(env: dict[str, str]) -> None:
    """Every tend-review COMMENT body would otherwise read as the maintainer
    telling the bot it was wrong."""
    _write(env, "CANDIDATES_JSON", [{"number": 7}])
    _write(env, "REVIEWS_JSON", [_review(IN_WINDOW, author=BOT)])

    assert _collect(env)["reviews"] == []


def test_a_deleted_review_author_is_still_a_human_correction(
    env: dict[str, str],
) -> None:
    review = _review(IN_WINDOW)
    review["user"] = None
    _write(env, "CANDIDATES_JSON", [{"number": 7}])
    _write(env, "REVIEWS_JSON", [review])

    (row,) = _collect(env)["reviews"]

    assert row["body"] == "wrong"


def test_no_candidate_prs_leaves_reviews_empty(env: dict[str, str]) -> None:
    """With no PR updated in the window the loop body never runs, so the
    reviews aggregation reduces over an empty stream."""
    out = _collect(env)

    assert out["reviews"] == []
    calls = Path(env["GH_CALLS"]).read_text()
    assert "/reviews" not in calls
