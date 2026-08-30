"""Tests for mention-verify.sh — tend-mention's engagement gate.

The gate decides whether a comment or review summons an agent session, so
every case here is an outward behaviour: a false negative leaves someone
un-answered, and a false positive burns a session that can only exit silently.
The script is inlined into the generated workflow (env in, GITHUB_OUTPUT out),
which is what lets these run it directly against a fake `gh`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

MENTION_VERIFY = (
    Path(__file__).resolve().parents[2]
    / "generator"
    / "src"
    / "tend"
    / "templates"
    / "mention-verify.sh"
)

BOT = "test-bot"

# `gh` stand-in. Each endpoint reads its fixture file, so a test reshapes one
# response by rewriting a file. Ordered longest-path-first: the review-comments
# path is a prefix-extension of the single-review path.
#
# PAGES emits the same page N times, which is what `--paginate` does to a
# `--jq`: the filter runs once per page rather than once over the union.
FAKE_GH = (
    GH_PREAMBLE
    + r"""
emit_paged() {
  local i=0
  while [ "$i" -lt "${PAGES:-1}" ]; do
    emit "$1"
    i=$((i + 1))
  done
}

case "$*" in
  "pr view "*)
    emit "$(cat "$PR_AUTHOR_JSON")"
    ;;
  *"/reviews/"*"/comments"*)
    emit_paged "$(cat "$INLINE_JSON")"
    ;;
  *"/pulls/comments/"*)
    [ -n "${MISSING_COMMENT:-}" ] && exit 1
    emit "$(cat "$COMMENT_JSON")"
    ;;
  *"/pulls/"*"/reviews/"*)
    [ -n "${MISSING_REVIEW:-}" ] && exit 1
    emit "$(cat "$REVIEW_JSON")"
    ;;
  *"/pulls/"*"/reviews"*)
    emit_paged "$(cat "$PR_REVIEWS_JSON")"
    ;;
  *"/issues/"*"/comments"*)
    emit_paged "$(cat "$ISSUE_COMMENTS_JSON")"
    ;;
  *)
    exit 1
    ;;
esac
"""
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A fake `gh` on PATH plus the workflow env the gate reads.

    Defaults describe the quietest world: no prior engagement, a PR someone
    else authored, an empty review. Each test writes only what it is about.
    """
    bindir = fake_bin(tmp_path, gh=FAKE_GH)

    files = {
        "REVIEW_JSON": {
            "user": {"login": "human"},
            "state": "COMMENTED",
            "body": "",
            "html_url": "https://github.com/owner/repo/pull/7#pullrequestreview-1",
            "submitted_at": "2026-01-02T03:04:05Z",
        },
        "COMMENT_JSON": {
            "user": {"login": "human"},
            "body": "",
            "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/7",
            "html_url": "https://github.com/owner/repo/pull/7#discussion_r1",
            "updated_at": "2026-01-02T03:04:05Z",
        },
        "INLINE_JSON": [],
        "ISSUE_COMMENTS_JSON": [],
        "PR_REVIEWS_JSON": [],
        "PR_AUTHOR_JSON": {"author": {"login": "human"}},
    }
    paths = {}
    for key, value in files.items():
        path = tmp_path / f"{key.lower()}.json"
        path.write_text(json.dumps(value))
        paths[key] = str(path)

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_REPOSITORY": "owner/repo",
        "BOT_NAME": BOT,
        # A relayed review dispatch, the path with the most judgement in it.
        "EVENT_NAME": "repository_dispatch",
        "PAYLOAD_KIND": "pull_request_review",
        "PAYLOAD_PR": "7",
        "PAYLOAD_ID": "1",
        "COMMENT_BODY": "",
        "COMMENT_AUTHOR": "",
        "COMMENT_AUTHOR_TYPE": "",
        "ISSUE_BODY": "",
        "ISSUE_OR_PR_NUMBER": "",
        "ISSUE_AUTHOR": "",
        "PR_URL": "",
        **paths,
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # `bash -e` mirrors the shell GitHub Actions gives a `run:` block.
    return subprocess.run(
        [BASH, "-e", str(MENTION_VERIFY)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _outputs(env: dict[str, str]) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in Path(env["GITHUB_OUTPUT"]).read_text().splitlines()
    )


def _verdict(env: dict[str, str]) -> tuple[str, str]:
    """(should_run, reason) as the handle job's `if:` and prompt read them."""
    result = _run(env)
    assert result.returncode == 0, result.stderr
    out = _outputs(env)
    assert "should_run" in out, out
    return out["should_run"], out.get("reason", "")


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["GH_CALLS"])
    return log.read_text().splitlines() if log.exists() else []


def _write(env: dict[str, str], key: str, value: object) -> None:
    Path(env[key]).write_text(json.dumps(value))


def _review(env: dict[str, str], **fields: object) -> None:
    record = json.loads(Path(env["REVIEW_JSON"]).read_text())
    _write(env, "REVIEW_JSON", {**record, **fields})


def _inline(env: dict[str, str], *comments: dict[str, object]) -> None:
    _write(env, "INLINE_JSON", list(comments))


def _fresh(body: str = "a note") -> dict[str, object]:
    """An inline comment as GitHub serves a fresh one: no `in_reply_to_id` key
    at all — absent rather than null, which is what the gate's object
    construction has to normalize."""
    return {"body": body, "id": 10}


def _reply(body: str = "a reply") -> dict[str, object]:
    return {"body": body, "id": 11, "in_reply_to_id": 10}


def _issue_comment(env: dict[str, str], **fields: str) -> None:
    """Re-aim the env at a direct `issue_comment` event."""
    env.update(
        {
            "EVENT_NAME": "issue_comment",
            "PAYLOAD_KIND": "",
            "PAYLOAD_PR": "",
            "PAYLOAD_ID": "",
            "COMMENT_AUTHOR": "human",
            "COMMENT_AUTHOR_TYPE": "User",
            "ISSUE_OR_PR_NUMBER": "7",
            "ISSUE_AUTHOR": "human",
            **fields,
        }
    )


# ---------------------------------------------------------------------------
# Resolving a relayed event: the dispatch payload is forgeable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pr", "review_id"),
    [("7; rm -rf /", "1"), ("7", "1 OR 1"), ("", "1"), ("7", "")],
)
def test_non_numeric_ids_are_refused_before_any_api_call(
    env: dict[str, str], pr: str, review_id: str
) -> None:
    """The ids are spliced into API paths here and into the handle prompt
    later, so anything but digits dies at this edge."""
    env["PAYLOAD_PR"] = pr
    env["PAYLOAD_ID"] = review_id

    assert _verdict(env) == ("false", "")
    assert not Path(env["GH_CALLS"]).exists(), "a malformed payload reached the API"


def test_unknown_dispatch_kind_is_refused(env: dict[str, str]) -> None:
    env["PAYLOAD_KIND"] = "pull_request"

    assert _verdict(env) == ("false", "")


def test_a_review_the_api_does_not_have_is_refused(env: dict[str, str]) -> None:
    """A forged dispatch can name any id; the fetch is what proves it exists."""
    env["MISSING_REVIEW"] = "1"

    assert _verdict(env) == ("false", "")


def test_a_comment_the_api_does_not_have_is_refused(env: dict[str, str]) -> None:
    """Without this branch a forged dispatch naming a nonexistent comment id
    reaches the PR-binding check with an empty `$COMMENT`."""
    env["PAYLOAD_KIND"] = "pull_request_review_comment"
    env["MISSING_COMMENT"] = "1"

    assert _verdict(env) == ("false", "")


def test_a_comment_belonging_to_another_pr_is_refused(env: dict[str, str]) -> None:
    """Inline comments fetch by id alone, so a payload pairing a real comment
    with an unrelated PR would otherwise steer the handle job at that PR."""
    env["PAYLOAD_KIND"] = "pull_request_review_comment"
    _write(
        env,
        "COMMENT_JSON",
        {
            "user": {"login": "human"},
            "body": f"@{BOT} look",
            "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/99",
            "html_url": "https://github.com/owner/repo/pull/99#discussion_r1",
            "updated_at": "2026-01-02T03:04:05Z",
        },
    )

    assert _verdict(env) == ("false", "")


def test_the_words_judged_come_from_the_api_not_the_payload(
    env: dict[str, str],
) -> None:
    """The body, url and timestamp all come off the fetched record — the
    payload carries identifiers only."""
    _review(env, body=f"@{BOT} please look")

    assert _verdict(env) == ("true", "mention")
    assert _outputs(env)["url"] == (
        "https://github.com/owner/repo/pull/7#pullrequestreview-1"
    )
    assert _outputs(env)["ts"] == "2026-01-02T03:04:05Z"


def test_a_pending_review_writes_an_empty_timestamp(env: dict[str, str]) -> None:
    """Only a forged dispatch can name a PENDING review, which has no
    `submitted_at`. The literal string `null` would reach handle's `date -d`
    and fail the job red where an empty value skips cleanly."""
    _review(env, submitted_at=None, body=f"@{BOT}")

    _verdict(env)

    assert _outputs(env)["ts"] == ""


# ---------------------------------------------------------------------------
# Being named
# ---------------------------------------------------------------------------


def test_an_edited_issue_always_runs(env: dict[str, str]) -> None:
    """The workflow's `if:` already required the mention in the issue body."""
    env["EVENT_NAME"] = "issues"

    assert _verdict(env) == ("true", "")


def test_a_mention_in_the_review_body_runs(env: dict[str, str]) -> None:
    _review(env, body=f"hey @{BOT}, thoughts?")

    assert _verdict(env) == ("true", "mention")


def test_a_mention_inside_an_inline_comment_runs(env: dict[str, str]) -> None:
    """The review record carries `review.body` but not its inline comments, so
    a first-contact mention written inside one is invisible without the
    separate fetch — and there is no prior engagement to fall back on."""
    _inline(env, _fresh(f"@{BOT} can you fix this?"))

    assert _verdict(env) == ("true", "mention")


def test_a_mention_wins_over_the_bot_review_gate(env: dict[str, str]) -> None:
    """The inline scan precedes the handoff gate, so an explicit summons the
    bot's own review quotes still fires."""
    _review(env, user={"login": BOT})
    _inline(env, _reply(f"@{BOT} one more thing"))

    assert _verdict(env) == ("true", "mention")


def test_the_bots_own_comment_never_summons_it_even_quoting_a_mention(
    env: dict[str, str],
) -> None:
    """The bot's PAT-based User account is invisible to the Bot-type skip, and
    the self-guard has to precede the mention check: a bot comment quoting a
    prior @-mention would otherwise re-match it."""
    _issue_comment(env, COMMENT_AUTHOR=BOT, COMMENT_BODY=f"you said: @{BOT} do X")

    assert _verdict(env) == ("false", "")


def test_the_bots_own_relayed_inline_comment_never_summons_it(
    env: dict[str, str],
) -> None:
    env["PAYLOAD_KIND"] = "pull_request_review_comment"
    _write(
        env,
        "COMMENT_JSON",
        {
            "user": {"login": BOT},
            "body": f"as @{BOT} noted above",
            "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/7",
            "html_url": "https://github.com/owner/repo/pull/7#discussion_r1",
            "updated_at": "2026-01-02T03:04:05Z",
        },
    )

    assert _verdict(env) == ("false", "")


def test_another_bots_undirected_comment_is_dropped(env: dict[str, str]) -> None:
    """Deploy and CI bots comment constantly, and edit those comments as
    status changes. On a bot-authored PR the engagement short-circuit below
    would boot a session for each one."""
    _issue_comment(
        env,
        COMMENT_AUTHOR="cloudflare-pages",
        COMMENT_AUTHOR_TYPE="Bot",
        COMMENT_BODY="Deploying... ✅",
        PR_URL="https://api.github.com/repos/owner/repo/pulls/7",
    )
    _write(env, "PR_AUTHOR_JSON", {"author": {"login": BOT}})

    assert _verdict(env) == ("false", "")


def test_another_bot_that_names_us_still_runs(env: dict[str, str]) -> None:
    """The Bot-type skip sits after the mention check, so a bot that addresses
    us directly is honoured."""
    _issue_comment(
        env,
        COMMENT_AUTHOR="some-bot",
        COMMENT_AUTHOR_TYPE="Bot",
        COMMENT_BODY=f"@{BOT} the deploy failed",
    )

    assert _verdict(env) == ("true", "mention")


# ---------------------------------------------------------------------------
# The bot's own review: reviewer role handing work to author role
# ---------------------------------------------------------------------------


def test_the_bots_review_on_its_own_pr_with_a_body_runs(env: dict[str, str]) -> None:
    """tend-review leaving a critique on a tend-authored PR is the reviewer
    role handing work to the author role, not a self-loop."""
    _review(env, user={"login": BOT}, body="This needs a test.")
    _write(env, "PR_AUTHOR_JSON", {"author": {"login": BOT}})

    assert _verdict(env) == ("true", "participation")


def test_the_bots_review_on_its_own_pr_with_a_fresh_inline_comment_runs(
    env: dict[str, str],
) -> None:
    """An empty-body review owning a non-reply inline comment still carries
    actionable signal. `in_reply_to_id` is absent rather than null on a fresh
    comment, so the count depends on the gate normalizing the two shapes."""
    _review(env, user={"login": BOT})
    _write(env, "PR_AUTHOR_JSON", {"author": {"login": BOT}})
    _inline(env, _fresh())

    assert _verdict(env) == ("true", "participation")


def test_the_bots_reply_container_on_its_own_pr_is_dropped(
    env: dict[str, str],
) -> None:
    """GitHub wraps an inline reply in a synthetic zero-body review. The bot's
    own reply arriving that way is the comment its `pull_request_review_comment`
    path already drops."""
    _review(env, user={"login": BOT})
    _write(env, "PR_AUTHOR_JSON", {"author": {"login": BOT}})
    _inline(env, _reply())

    assert _verdict(env) == ("false", "")


def test_the_bots_review_on_someone_elses_pr_is_dropped(env: dict[str, str]) -> None:
    """The tend-review session that submitted it already did whatever the
    review warranted. This subsumes the empty-body approval case: GitHub
    rejects self-approvals, so a bot approval only ever exists here."""
    _review(env, user={"login": BOT}, state="APPROVED", body="Looks good, but see #4.")

    assert _verdict(env) == ("false", "")


def test_a_humans_reply_container_on_a_bot_pr_still_runs(env: dict[str, str]) -> None:
    """The gate is keyed on the *bot* as review author, and that narrowing is
    load-bearing: `pull_request_review_comment` subscribes to `edited` only, so
    a human's freshly created inline reply reaches the bot only through this
    container. An authorship-agnostic gate would silence the bot on every human
    reply to its own review threads."""
    _write(env, "PR_AUTHOR_JSON", {"author": {"login": BOT}})
    _inline(env, _reply("what about the timeout?"))

    assert _verdict(env) == ("true", "participation")


# ---------------------------------------------------------------------------
# Contentless approvals are terminal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["APPROVED", "approved"])
def test_a_bare_approval_asks_for_nothing(env: dict[str, str], state: str) -> None:
    """No body, no inline comments, and the bot cannot merge on its own. REST
    reports the state uppercase where a webhook payload's is lowercase, so the
    gate has to read one normalized shape."""
    _review(env, state=state)
    _write(env, "PR_AUTHOR_JSON", {"author": {"login": BOT}})

    assert _verdict(env) == ("false", "")


def test_an_approval_with_inline_nits_runs(env: dict[str, str]) -> None:
    """Nits addressed to the PR's author — a role the bot has to act in on its
    own PRs — regardless of whether they are fresh or replies."""
    _review(env, state="APPROVED")
    _write(env, "PR_AUTHOR_JSON", {"author": {"login": BOT}})
    _inline(env, _reply("still worth renaming"))

    assert _verdict(env) == ("true", "participation")


def test_a_bare_commented_review_is_not_terminal(env: dict[str, str]) -> None:
    """Only `approved` is terminal: a bare CHANGES_REQUESTED or COMMENTED
    review is how GitHub delivers a human's inline reply, so widening the state
    would silence the bot on replies to its own review threads."""
    _review(env, state="CHANGES_REQUESTED")
    _write(env, "PR_AUTHOR_JSON", {"author": {"login": BOT}})

    assert _verdict(env) == ("true", "participation")


# ---------------------------------------------------------------------------
# Engagement heuristics for comments
# ---------------------------------------------------------------------------


def test_a_comment_on_a_bot_authored_issue_runs(env: dict[str, str]) -> None:
    _issue_comment(env, ISSUE_AUTHOR=BOT, COMMENT_BODY="any update?")

    assert _verdict(env) == ("true", "")


def test_a_comment_on_an_issue_that_names_the_bot_runs(env: dict[str, str]) -> None:
    """The mention lives in the issue *body*, so every comment on that issue is
    part of a conversation the bot was invited into."""
    _issue_comment(env, ISSUE_BODY=f"cc @{BOT}", COMMENT_BODY="any update?")

    assert _verdict(env) == ("true", "")


def test_a_comment_on_an_issue_the_bot_has_spoken_in_runs(env: dict[str, str]) -> None:
    _issue_comment(env, COMMENT_BODY="any update?")
    _write(env, "ISSUE_COMMENTS_JSON", [{"user": {"login": BOT}, "id": 3}])

    assert _verdict(env) == ("true", "")


def test_a_comment_on_an_unrelated_issue_is_dropped(env: dict[str, str]) -> None:
    _issue_comment(env, COMMENT_BODY="two humans talking")

    assert _verdict(env) == ("false", "")


@pytest.mark.parametrize(
    ("fixture", "record"),
    [
        ("PR_REVIEWS_JSON", {"user": {"login": BOT}, "id": 5}),
        ("ISSUE_COMMENTS_JSON", {"user": {"login": BOT}, "id": 6}),
    ],
)
def test_a_pr_comment_where_the_bot_has_engaged_runs(
    env: dict[str, str], fixture: str, record: dict[str, object]
) -> None:
    _issue_comment(
        env,
        COMMENT_BODY="what do you think?",
        PR_URL="https://api.github.com/repos/owner/repo/pulls/7",
    )
    _write(env, fixture, [record])

    assert _verdict(env) == ("true", "participation")


def test_a_third_party_review_on_a_pr_the_bot_reviewed_runs(
    env: dict[str, str],
) -> None:
    """The engagement path a review dispatch takes, with the PR number coming
    off the payload rather than the issue event."""
    _review(env, body="I disagree with the reviewer here.")
    _write(env, "PR_REVIEWS_JSON", [{"user": {"login": BOT}, "id": 5}])

    assert _verdict(env) == ("true", "participation")


def test_an_issue_comment_never_reaches_the_review_endpoints(
    env: dict[str, str],
) -> None:
    """PAYLOAD_PR and PAYLOAD_ID are empty on a directly-delivered comment, so
    a review fetch not gated on the review kind would request
    `/pulls//reviews//comments` — a 404 that fails the whole job red rather
    than skipping."""
    _issue_comment(
        env,
        COMMENT_BODY="two humans talking",
        PR_URL="https://api.github.com/repos/owner/repo/pulls/7",
    )

    _verdict(env)

    calls = _calls(env)
    assert calls, "the engagement lookups never ran"
    assert not any("/reviews/" in call for call in calls), calls
    assert not any("//" in call.split(" --jq ")[0] for call in calls), calls


def test_a_pr_comment_with_no_engagement_is_dropped(env: dict[str, str]) -> None:
    _issue_comment(
        env,
        COMMENT_BODY="two humans talking",
        PR_URL="https://api.github.com/repos/owner/repo/pulls/7",
    )

    assert _verdict(env) == ("false", "")


def test_engagement_survives_a_paginated_lookup(env: dict[str, str]) -> None:
    """`gh api --paginate` applies `--jq` once per page. A reducing filter
    (`| length`) would leave `100\\n7` in the variable, a numeric test on it
    errors with `integer expression expected`, and the failed test falls
    through to should_run=false — the bot going quiet on exactly the threads it
    has engaged with most, with nothing going red."""
    env["PAGES"] = "2"
    _issue_comment(
        env,
        COMMENT_BODY="what do you think?",
        PR_URL="https://api.github.com/repos/owner/repo/pulls/7",
    )
    _write(env, "ISSUE_COMMENTS_JSON", [{"user": {"login": BOT}, "id": 6}])

    assert _verdict(env) == ("true", "participation")


def test_paginated_lookups_never_reduce_inside_jq() -> None:
    """The hazard above is textual: with the `-n` guard in place both filter
    shapes answer the same on a two-page fixture, so only the source pins it.
    Reducing through a pipe (`| wc -l`) is no escape either — it moves the
    substitution's exit status off `gh`, so under `bash -e` a failed API call
    reads as "no engagement" on a green job."""
    source = MENTION_VERIFY.read_text()

    for line in source.replace("\\\n", " ").splitlines():
        if "--paginate" not in line or "--jq" not in line:
            continue
        assert "| length" not in line, (
            f"reduction inside a --paginate'd --jq runs per page: {line.strip()}"
        )
    assert source.count('| .id"') == 3, (
        "the three engagement lookups (issue comments, PR reviews, PR comments) "
        "must capture the raw stream, so a failing `gh api` still trips errexit"
    )
