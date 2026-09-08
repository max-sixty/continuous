# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Reduce bot reviews and timeline events to canonical coverage and recovery state.

GitHub's native review fields carry author, commit, state, and submission time.
A submitted bot review covers its commit when it has reader-facing content, an
inline finding, an approval, or Tend's marker for a deliberately silent pass.
Pending reviews never cover. GitHub's native dismissal timeline records
invalidate earlier coverage on the same commit, so a failed replacement remains
recoverable; a dismissal remains review context until a later bot approval.
Ready-for-review is a separate generation latch: only a submitted bot review
naming the exact timeline event acknowledges it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import github_cli

_READY_REVIEW_RE = re.compile(r"<!-- tend:ready-review:([1-9][0-9]*) -->")
_REVIEW_COMPLETE_RE = re.compile(r"<!-- tend:review-complete -->")
_REVIEW_OPERATION_RE = re.compile(
    r"<!-- tend:review-operation:([0-9a-f]{32}):(draft|full) -->"
)
_RESERVED_REVIEW_MARKER_RE = re.compile(
    r"<!--[ \t]*tend:(?:draft-review(?:[ \t]*-->|:.*?-->)|"
    r"review-complete[ \t]*-->|(?:ready-review|review-operation):.*?-->)",
    re.DOTALL,
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


def review_complete_marker() -> str:
    """Return the marker recording an intentionally silent completed pass."""
    return "<!-- tend:review-complete -->"


def review_operation_marker(operation_id: str, review_mode: str) -> str:
    """Return the marker identifying one private pending-review operation."""
    marker = f"<!-- tend:review-operation:{operation_id}:{review_mode} -->"
    if _REVIEW_OPERATION_RE.fullmatch(marker) is None:
        raise ValueError(
            "review operation needs a 32-digit lowercase hexadecimal ID and "
            "draft or full mode"
        )
    return marker


def strip_review_metadata(body: str) -> str:
    """Remove Tend's reserved metadata from reader-facing review prose."""
    # Keep a separator at each deletion boundary: deleting an inner marker
    # must not join its surrounding text into a new, valid reserved marker.
    return _RESERVED_REVIEW_MARKER_RE.sub(" ", body).strip()


def _ready_review_ids(body: str) -> set[int]:
    return {int(match) for match in _READY_REVIEW_RE.findall(body)}


def _is_completed_review(body: str) -> bool:
    return any(
        _REVIEW_COMPLETE_RE.fullmatch(line.strip()) is not None
        for line in body.splitlines()
    )


def _review_operation(body: str) -> tuple[str, str] | None:
    matches = _REVIEW_OPERATION_RE.findall(body)
    return matches[0] if len(matches) == 1 else None


def _pending_review_operation(review: dict[str, Any]) -> tuple[str, str] | None:
    if review.get("state") != "PENDING" or review.get("submitted_at") is not None:
        return None
    return _review_operation(review.get("body") or "")


def _submitted_bot_reviews(
    reviews: list[dict[str, Any]], bot: str
) -> list[dict[str, Any]]:
    return [
        review
        for review in reviews
        if github_cli.actor_login(review.get("user")) == bot
        and review.get("submitted_at") is not None
    ]


def _pending_reviews(
    reviews: list[dict[str, Any]], bot: str
) -> list[tuple[dict[str, Any], str, str]]:
    return [
        (review, operation_id, review_mode)
        for review in reviews
        if github_cli.actor_login(review.get("user")) == bot
        if (operation := _pending_review_operation(review)) is not None
        for operation_id, review_mode in [operation]
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


def _bot_review_dismissals(
    events: list[dict[str, Any]], mine: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    reviews_by_id = {review.get("id"): review for review in mine}
    parsed = []
    for event in events:
        event_id = event.get("id")
        created_at = event.get("created_at")
        dismissed = event.get("dismissed_review")
        if (
            isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or event_id <= 0
            or not isinstance(created_at, str)
            or not created_at
            or not isinstance(dismissed, dict)
        ):
            raise ValueError(
                "review_dismissed timeline event is missing a valid id/time"
            )
        raw_review_id = dismissed.get("review_id")
        if isinstance(raw_review_id, bool) or not (
            isinstance(raw_review_id, int)
            or (isinstance(raw_review_id, str) and raw_review_id.isdigit())
        ):
            raise ValueError("review_dismissed timeline event has an invalid review id")
        review_id = int(raw_review_id)
        if review_id not in reviews_by_id:
            continue
        review = reviews_by_id[review_id]
        message = dismissed.get("dismissal_message")
        if message is not None and not isinstance(message, str):
            raise ValueError("review_dismissed timeline event has an invalid message")
        prior_state = dismissed.get("state")
        if prior_state is not None and not isinstance(prior_state, str):
            raise ValueError("review_dismissed timeline event has an invalid state")
        dismissal_commit_id = dismissed.get("dismissal_commit_id")
        if dismissal_commit_id is not None and not isinstance(dismissal_commit_id, str):
            raise ValueError(
                "review_dismissed timeline event has an invalid dismissal commit"
            )
        parsed.append(
            {
                "id": event_id,
                "review_id": review_id,
                "sha": review.get("commit_id"),
                "at": created_at,
                "message": message or "",
                "prior_state": prior_state or "",
                "dismissal_commit_id": dismissal_commit_id or "",
            }
        )
    parsed_ids = {event["review_id"] for event in parsed}
    missing = [
        review["id"]
        for review in mine
        if review.get("state") == "DISMISSED" and review.get("id") not in parsed_ids
    ]
    if missing:
        raise ValueError(
            "dismissed bot review is missing timeline metadata: "
            + ", ".join(str(review_id) for review_id in missing)
        )
    return sorted(parsed, key=lambda event: (event["at"], event["id"]))


def review_state(
    *,
    head_sha: str,
    bot: str,
    substantive_ids: set[int],
    force_push_times: list[str],
    ready_for_review_events: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    review_dismissal_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reduce GitHub review records to the state used by Tend's skills."""
    mine = _submitted_bot_reviews(reviews, bot)
    pending = _pending_reviews(reviews, bot)
    dismissals = _bot_review_dismissals(review_dismissal_events or [], mine)

    def review_is_after(review: dict[str, Any], event: dict[str, Any]) -> bool:
        # GitHub timestamps have one-second precision, so equality cannot prove
        # which outward action won. Treat it as uncovered and retry.
        return (review.get("submitted_at") or "") > event["at"]

    def public_body(review: dict[str, Any]) -> str:
        return strip_review_metadata(review.get("body") or "")

    substantive = [
        review
        for review in mine
        if review.get("state") != "DISMISSED"
        and (
            public_body(review)
            or review.get("id") in substantive_ids
            or review.get("state") == "APPROVED"
        )
    ]
    last_substantive = substantive[-1] if substantive else None
    completed = [
        review
        for review in mine
        if review.get("state") != "DISMISSED"
        and _is_completed_review(review.get("body") or "")
    ]
    coverage_candidates = [
        review for review in mine if review in substantive or review in completed
    ]
    covered = [
        review
        for review in mine
        if review in coverage_candidates
        if not any(
            dismissal["sha"] == review.get("commit_id")
            and not review_is_after(review, dismissal)
            for dismissal in dismissals
        )
    ]
    last_covered = covered[-1] if covered else None
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
        for review in covered
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
    latest_dismissal = dismissals[-1] if dismissals else None
    standing_dismissal = (
        latest_dismissal
        if latest_dismissal
        and not any(
            review.get("state") == "APPROVED"
            and review_is_after(review, latest_dismissal)
            for review in mine
        )
        else None
    )

    latest_ready = _latest_ready_for_review(ready_for_review_events)
    acknowledged_ready_ids = sorted(
        {
            event_id
            for review in mine
            for event_id in _ready_review_ids(review.get("body") or "")
        }
    )
    outstanding_ready = (
        latest_ready
        if latest_ready and latest_ready["id"] not in acknowledged_ready_ids
        else None
    )
    pending_records = [
        {
            "id": review["id"],
            "sha": review.get("commit_id"),
            "operation_id": operation_id,
            "draft_mode": review_mode == "draft",
            "ready_review_ids": sorted(_ready_review_ids(review.get("body") or "")),
            "body": public_body(review),
        }
        for review, operation_id, review_mode in pending
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
            }
            if last_substantive
            else None
        ),
        "last_covered": (
            {
                "id": last_covered["id"],
                "sha": last_covered["commit_id"],
                "state": last_covered["state"],
                "at": last_covered.get("submitted_at"),
            }
            if last_covered
            else None
        ),
        "force_pushed_since": bool(last_covered and not after_rewrite(last_covered)),
        "at_head": (
            {
                "id": at_head[-1]["id"],
                "state": at_head[-1]["state"],
                "at": at_head[-1].get("submitted_at"),
            }
            if at_head
            else None
        ),
        "latest_ready_for_review": latest_ready,
        "acknowledged_ready_ids": acknowledged_ready_ids,
        "outstanding_ready_for_review": outstanding_ready,
        "pending_reviews": pending_records,
        "needs_review": not at_head or outstanding_ready is not None,
        "fresh_approval_sha": (
            (fresh_approvals[-1].get("commit_id") or "") if fresh_approvals else ""
        ),
        "stale_approval_id": (
            last_approval["id"]
            if last_approval and last_force_push_at and not after_rewrite(last_approval)
            else ""
        ),
        "standing_approval_id": (standing_approval["id"] if standing_approval else ""),
        "standing_dismissal": (
            {
                "event_id": standing_dismissal["id"],
                "review_id": standing_dismissal["review_id"],
                "sha": standing_dismissal["sha"],
                "at": standing_dismissal["at"],
                "message": standing_dismissal["message"],
                "prior_state": standing_dismissal["prior_state"],
                "dismissal_commit_id": standing_dismissal["dismissal_commit_id"],
            }
            if standing_dismissal
            else None
        ),
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
    reviews = github_cli.paginated(
        "api", "--paginate", f"repos/{repo}/pulls/{pr}/reviews"
    )
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
    review_dismissal_events = [
        event for event in timeline if event.get("event") == "review_dismissed"
    ]
    return review_state(
        head_sha=head,
        bot=bot,
        substantive_ids=substantive_ids,
        force_push_times=force_push_times,
        ready_for_review_events=ready_for_review_events,
        reviews=reviews,
        review_dismissal_events=review_dismissal_events,
    )


def request_review(pr: str) -> None:
    """Dispatch the serialized review workflow when canonical demand remains."""
    repo = github_cli.repository()
    state = fetch_review_state(pr, repo=repo)
    if not state["needs_review"]:
        print(f"skip: PR #{pr} has no outstanding review demand")
        return
    github_cli.run(
        "api",
        f"repos/{repo}/dispatches",
        "--method",
        "POST",
        "--input",
        "-",
        input=json.dumps(
            {
                "event_type": "tend-review",
                "client_payload": {"pr_number": int(pr)},
            }
        ),
    )
    print(f"requested: review for PR #{pr}")


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
    reviews = github_cli.paginated(
        "api", "--paginate", f"repos/{repo}/pulls/{pr}/reviews?per_page=100"
    )
    pending = _pending_reviews(reviews, bot)
    pr_view = github_cli.json_call(
        "pr", "view", pr, "--repo", repo, "--json", "comments,reviews"
    )
    inline = [
        (comment, comment.get("pullRequestReview") or {})
        for thread in _review_threads(pr, repo, FEEDBACK_QUERY)
        for comment in thread["comments"]["nodes"]
        if github_cli.actor_login(comment.get("author")) == bot
    ]

    def pending_parent(review: dict[str, Any]) -> bool:
        return (
            github_cli.actor_login(review.get("author")) == bot
            and review.get("state") == "PENDING"
            and _review_operation(review.get("body") or "") is not None
        )

    previous_reviews = []
    for review in pr_view["reviews"]:
        body = strip_review_metadata(review.get("body") or "")
        if (
            github_cli.actor_login(review.get("author")) == bot
            and review.get("state") != "PENDING"
            and body
        ):
            previous_reviews.append(
                {
                    "state": review.get("state"),
                    "submitted_at": review.get("submittedAt"),
                    "body": body,
                }
            )
    pending_inline = [
        {
            "review_id": review["id"],
            "path": comment.get("path"),
            "line": comment.get("line"),
            "created_at": comment.get("created_at"),
            "body": comment.get("body"),
        }
        for review, _operation_id, _review_mode in pending
        for comment in github_cli.paginated(
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr}/reviews/{review['id']}/comments?per_page=100",
        )
    ]
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
            if not pending_parent(review)
        ],
        "pending_reviews": [
            {
                "review_id": review["id"],
                "sha": review.get("commit_id"),
                "review_mode": review_mode,
                "ready_review_ids": sorted(_ready_review_ids(review.get("body") or "")),
                "body": strip_review_metadata(review.get("body") or ""),
            }
            for review, _operation_id, review_mode in pending
        ],
        "pending_inline_comments": pending_inline,
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
    if len(args) == 2 and args[0] == "request" and args[1].isdigit():
        request_review(args[1])
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
        f"usage: {sys.argv[0]} state|request|feedback|threads|prepare-approval "
        "<pr-number> | "
        "dismiss|dismiss-stale <pr-number> <message> | resolve-thread <thread-id>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
    except ValueError as error:
        print(f"bot-review-state: {error}", file=sys.stderr)
        raise SystemExit(1) from None
