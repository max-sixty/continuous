# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Reduce bot reviews and timeline events to canonical coverage and recovery state."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import github_cli

DRAFT_REVIEW_MARKER = "<!-- tend:draft-review -->"
LEGACY_DRAFT_REVIEW_PREFIX = "Reviewing as a draft —"
_READY_REVIEW_RE = re.compile(r"<!-- tend:ready-review:([1-9][0-9]*) -->")
_INCOMPLETE_REVIEW_RE = re.compile(r"<!-- tend:review-incomplete:([0-9a-f]{32}) -->")
_RESERVED_REVIEW_MARKER_RE = re.compile(
    r"<!--[ \t]*tend:(?:ready-review|review-incomplete):.*?-->", re.DOTALL
)
FEEDBACK_QUERY = """
query($owner:String!,$repo:String!,$number:Int!) {
  repository(owner:$owner,name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        nodes { comments(first:100) { nodes {
          author { login } path line body createdAt
          pullRequestReview { author { login } body state fullDatabaseId }
        } } }
      }
    }
  }
}
"""
THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes { author { login } path line body }
          }
        }
      }
    }
  }
}
"""
RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id }
  }
}
"""


def ready_review_marker(event_id: int) -> str:
    """Return the marker acknowledging one exact ready-for-review event."""
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        raise ValueError("ready-for-review event ID must be a positive integer")
    return f"<!-- tend:ready-review:{event_id} -->"


def incomplete_review_marker(operation_id: str) -> str:
    """Return the marker identifying one unfinished inline-review submission."""
    marker = f"<!-- tend:review-incomplete:{operation_id} -->"
    if _INCOMPLETE_REVIEW_RE.fullmatch(marker) is None:
        raise ValueError("review operation ID must be 32 lowercase hexadecimal digits")
    return marker


def strip_review_metadata(body: str) -> str:
    """Remove Tend's private submission markers from a public review body."""
    return _RESERVED_REVIEW_MARKER_RE.sub("", body).strip()


def _ready_review_ids(body: str) -> set[int]:
    return {int(match) for match in _READY_REVIEW_RE.findall(body)}


def _incomplete_operation_id(body: str) -> str | None:
    matches = _INCOMPLETE_REVIEW_RE.findall(body)
    return matches[0] if len(matches) == 1 else None


def _review_incomplete_operation_id(review: dict[str, Any]) -> str | None:
    if review.get("state") != "COMMENTED":
        return None
    return _incomplete_operation_id(review.get("body") or "")


def _submitted_bot_reviews(
    reviews: list[dict[str, Any]], bot: str
) -> list[dict[str, Any]]:
    return [
        review
        for review in reviews
        if github_cli.actor_login(review.get("user")) == bot
        and review.get("submitted_at") is not None
    ]


def _incomplete_reviews(
    reviews: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str]]:
    return [
        (review, operation_id)
        for review in reviews
        if (operation_id := _review_incomplete_operation_id(review)) is not None
    ]


def _latest_ready_for_review(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    parsed = []
    for event in events:
        event_id = event.get("id")
        created_at = event.get("created_at")
        if (
            isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or event_id <= 0
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise ValueError(
                "ready_for_review timeline event is missing a valid id/time"
            )
        parsed.append({"id": event_id, "at": created_at})
    return max(parsed, key=lambda event: (event["at"], event["id"])) if parsed else None


def review_state(
    *,
    head_sha: str,
    bot: str,
    substantive_ids: set[int],
    force_push_times: list[str],
    ready_for_review_events: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reduce GitHub review records to the state used by Tend's skills."""
    mine = _submitted_bot_reviews(reviews, bot)
    incomplete = _incomplete_reviews(mine)
    incomplete_ids = {review["id"] for review, _operation_id in incomplete}

    def public_body(review: dict[str, Any]) -> str:
        return strip_review_metadata(review.get("body") or "")

    substantive = [
        review
        for review in mine
        if review.get("id") not in incomplete_ids
        and (
            public_body(review)
            or review.get("id") in substantive_ids
            or review.get("state") == "APPROVED"
        )
    ]
    last_substantive = substantive[-1] if substantive else None
    approvals = [review for review in mine if review.get("state") == "APPROVED"]
    last_approval = approvals[-1] if approvals else None
    last_force_push_at = max(force_push_times, default="")

    def after_rewrite(review: dict[str, Any]) -> bool:
        return (
            not last_force_push_at
            or (review.get("submitted_at") or "") > last_force_push_at
        )

    at_head = [
        review
        for review in substantive
        if review.get("commit_id") == head_sha and after_rewrite(review)
    ]
    fresh_approvals = [review for review in approvals if after_rewrite(review)]
    decisions = [
        review
        for review in mine
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED"}
    ]
    standing_approval = (
        decisions[-1]
        if decisions and decisions[-1].get("state") == "APPROVED"
        else None
    )

    def is_draft_review(review: dict[str, Any]) -> bool:
        body = public_body(review)
        # TODO(2026-12-01): Drop the prose-prefix fallback after pre-marker
        # reviews have aged out.
        return review.get("state") == "COMMENTED" and (
            DRAFT_REVIEW_MARKER in body or body.startswith(LEGACY_DRAFT_REVIEW_PREFIX)
        )

    latest_ready = _latest_ready_for_review(ready_for_review_events)
    acknowledged_ready_ids = sorted(
        {
            event_id
            for review in substantive
            if not is_draft_review(review)
            for event_id in _ready_review_ids(review.get("body") or "")
        }
    )
    outstanding_ready = (
        latest_ready
        if latest_ready and latest_ready["id"] not in acknowledged_ready_ids
        else None
    )
    incomplete_at_head = [
        {
            "id": review["id"],
            "sha": review.get("commit_id"),
            "at": review.get("submitted_at"),
            "operation_id": operation_id,
            "draft_mode": is_draft_review(review),
            "ready_review_ids": sorted(_ready_review_ids(review.get("body") or "")),
        }
        for review, operation_id in incomplete
        if review.get("commit_id") == head_sha and after_rewrite(review)
    ]

    return {
        "head_sha": head_sha,
        "bot_login": bot,
        "last_force_push_at": last_force_push_at,
        "last_substantive": (
            {
                "id": last_substantive["id"],
                "sha": last_substantive["commit_id"],
                "state": last_substantive["state"],
                "at": last_substantive.get("submitted_at"),
                "draft_mode": is_draft_review(last_substantive),
            }
            if last_substantive
            else None
        ),
        "force_pushed_since": bool(
            last_substantive
            and last_force_push_at
            and last_force_push_at > (last_substantive.get("submitted_at") or "")
        ),
        "at_head": (
            {
                "id": at_head[-1]["id"],
                "state": at_head[-1]["state"],
                "at": at_head[-1].get("submitted_at"),
                "draft_mode": is_draft_review(at_head[-1]),
            }
            if at_head
            else None
        ),
        "latest_ready_for_review": latest_ready,
        "acknowledged_ready_ids": acknowledged_ready_ids,
        "outstanding_ready_for_review": outstanding_ready,
        "incomplete_reviews": incomplete_at_head,
        "fresh_approval_sha": (
            (fresh_approvals[-1].get("commit_id") or "") if fresh_approvals else ""
        ),
        "stale_approval_id": (
            last_approval["id"]
            if last_approval
            and last_force_push_at
            and (last_approval.get("submitted_at") or "") < last_force_push_at
            else ""
        ),
        "standing_approval_id": (standing_approval["id"] if standing_approval else ""),
    }


def fetch_review_state(pr: str, *, repo: str | None = None) -> dict[str, Any]:
    """Fetch and reduce the review state for *pr*."""
    repo = repo or github_cli.repository()
    bot = str(github_cli.json_call("api", "user")["login"])
    head = github_cli.json_call(
        "pr", "view", pr, "--repo", repo, "--json", "headRefOid"
    )["headRefOid"]
    comments = github_cli.paginated(
        "api", "--paginate", f"repos/{repo}/pulls/{pr}/comments"
    )
    substantive_ids = {
        int(comment["pull_request_review_id"])
        for comment in comments
        if comment.get("in_reply_to_id") is None
        and comment.get("pull_request_review_id") is not None
    }
    timeline = github_cli.paginated(
        "api", "--paginate", f"repos/{repo}/issues/{pr}/timeline"
    )
    force_push_times = [
        event["created_at"]
        for event in timeline
        if event.get("event") == "head_ref_force_pushed"
    ]
    ready_for_review_events = [
        event for event in timeline if event.get("event") == "ready_for_review"
    ]
    reviews = github_cli.paginated(
        "api", "--paginate", f"repos/{repo}/pulls/{pr}/reviews"
    )
    return review_state(
        head_sha=head,
        bot=bot,
        substantive_ids=substantive_ids,
        force_push_times=force_push_times,
        ready_for_review_events=ready_for_review_events,
        reviews=reviews,
    )


def dismiss_standing_approval(pr: str, message: str) -> None:
    """Dismiss the bot approval currently deciding *pr*, when one exists."""
    repo = github_cli.repository()
    state = fetch_review_state(pr, repo=repo)
    review_id = state["standing_approval_id"]
    if review_id:
        github_cli.run(
            "api",
            f"repos/{repo}/pulls/{pr}/reviews/{review_id}/dismissals",
            "-X",
            "PUT",
            "-f",
            f"message={message}",
        )


def prepare_approval(pr: str) -> dict[str, Any]:
    """Pin the live head unless the bot already approved that exact commit."""
    state = fetch_review_state(pr)
    head_sha = str(state["head_sha"])
    pin = Path(os.environ.get("CHECKED_HEAD_DIR", "/tmp")) / f"checked-head-{pr}"
    pin.unlink(missing_ok=True)
    already_approved = state.get("fresh_approval_sha") == head_sha
    result = {"head_sha": head_sha, "already_approved": already_approved}
    github_cli.dump(result)
    sys.stdout.flush()
    if not already_approved:
        pin.write_text(f"{head_sha}\n")
    return result


def dismiss_stale_approval(pr: str, message: str) -> None:
    """Dismiss the approval that predates the PR's newest force push, if any."""
    repo = github_cli.repository()
    state = fetch_review_state(pr, repo=repo)
    review_id = state["stale_approval_id"]
    if review_id:
        github_cli.run(
            "api",
            f"repos/{repo}/pulls/{pr}/reviews/{review_id}/dismissals",
            "-X",
            "PUT",
            "-f",
            f"message={message}",
        )


def _review_threads(pr: str, repo: str, query: str) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    response = github_cli.json_call(
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-f",
        f"owner={owner}",
        "-f",
        f"repo={name}",
        "-F",
        f"number={pr}",
    )
    return response["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]


def feedback(pr: str) -> dict[str, Any]:
    """Return prior public bot feedback and the PR conversation."""
    repo = github_cli.repository()
    bot = str(github_cli.json_call("api", "user")["login"])
    pr_view = github_cli.json_call(
        "pr", "view", pr, "--repo", repo, "--json", "comments,reviews"
    )
    inline = [
        (comment, comment.get("pullRequestReview") or {})
        for thread in _review_threads(pr, repo, FEEDBACK_QUERY)
        for comment in thread["comments"]["nodes"]
        if github_cli.actor_login(comment.get("author")) == bot
    ]

    def incomplete_parent(review: dict[str, Any]) -> bool:
        return (
            github_cli.actor_login(review.get("author")) == bot
            and _review_incomplete_operation_id(review) is not None
        )

    previous_reviews = []
    for review in pr_view["reviews"]:
        body = strip_review_metadata(review.get("body") or "")
        if (
            github_cli.actor_login(review.get("author")) == bot
            and _review_incomplete_operation_id(review) is None
            and body
        ):
            previous_reviews.append(
                {
                    "state": review.get("state"),
                    "submitted_at": review.get("submittedAt"),
                    "body": body,
                }
            )
    return {
        "previous_reviews": previous_reviews,
        "conversation": [
            {
                "author": github_cli.actor_login(comment.get("author")),
                "created_at": comment.get("createdAt"),
                "body": comment.get("body"),
            }
            for comment in pr_view["comments"]
        ],
        "inline_comments": [
            {
                "path": comment.get("path"),
                "line": comment.get("line"),
                "created_at": comment.get("createdAt"),
                "body": comment.get("body"),
            }
            for comment, review in inline
            if not incomplete_parent(review)
        ],
        "incomplete_inline_comments": [
            {
                "review_id": review.get("fullDatabaseId"),
                "path": comment.get("path"),
                "line": comment.get("line"),
                "created_at": comment.get("createdAt"),
                "body": comment.get("body"),
            }
            for comment, review in inline
            if incomplete_parent(review)
        ],
    }


def unresolved_threads(pr: str) -> list[dict[str, Any]]:
    """Return unresolved review threads whose first comment belongs to the bot."""
    repo = github_cli.repository()
    bot = str(github_cli.json_call("api", "user")["login"])
    result = []
    for thread in _review_threads(pr, repo, THREADS_QUERY):
        comments = thread["comments"]["nodes"]
        first = comments[0] if comments else None
        if (
            thread.get("isResolved") is False
            and first
            and github_cli.actor_login(first.get("author")) == bot
        ):
            result.append(
                {
                    "id": thread["id"],
                    "path": first.get("path"),
                    "line": first.get("line"),
                    "body": first.get("body"),
                }
            )
    return result


def resolve_thread(thread_id: str) -> None:
    """Resolve one review thread by its GraphQL node id."""
    github_cli.run(
        "api",
        "graphql",
        "-f",
        f"query={RESOLVE_THREAD_MUTATION}",
        "-f",
        f"threadId={thread_id}",
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "state" and args[1].isdigit():
        github_cli.dump(fetch_review_state(args[1]))
        return 0
    if len(args) == 3 and args[0] == "dismiss" and args[1].isdigit() and args[2]:
        dismiss_standing_approval(args[1], args[2])
        return 0
    if len(args) == 2 and args[0] == "prepare-approval" and args[1].isdigit():
        prepare_approval(args[1])
        return 0
    if len(args) == 3 and args[0] == "dismiss-stale" and args[1].isdigit() and args[2]:
        dismiss_stale_approval(args[1], args[2])
        return 0
    if len(args) == 2 and args[0] == "feedback" and args[1].isdigit():
        github_cli.dump(feedback(args[1]))
        return 0
    if len(args) == 2 and args[0] == "threads" and args[1].isdigit():
        github_cli.dump(unresolved_threads(args[1]))
        return 0
    if len(args) == 2 and args[0] == "resolve-thread" and args[1]:
        resolve_thread(args[1])
        return 0
    print(
        f"usage: {sys.argv[0]} state|feedback|threads|prepare-approval <pr-number> | "
        "dismiss|dismiss-stale <pr-number> <message> | resolve-thread <thread-id>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
