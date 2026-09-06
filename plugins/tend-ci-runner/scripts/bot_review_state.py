# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Resolve which bot review, if any, anchors a pull request's current head."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import github_cli

DRAFT_PREFIX = "Reviewing as a draft —"
FEEDBACK_QUERY = """
query($owner:String!,$repo:String!,$number:Int!) {
  repository(owner:$owner,name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        nodes { comments(first:100) { nodes {
          author { login } path line body createdAt
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


def review_state(
    *,
    head_sha: str,
    bot: str,
    substantive_ids: set[int],
    force_push_times: list[str],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reduce GitHub review records to the state used by Tend's skills."""
    mine = [
        review
        for review in reviews
        if github_cli.actor_login(review.get("user")) == bot
        and review.get("submitted_at") is not None
    ]
    substantive = [
        review
        for review in mine
        if review.get("body")
        or review.get("id") in substantive_ids
        or review.get("state") == "APPROVED"
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
    body_at_head = [review for review in at_head if review.get("body")]
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
                "draft_mode": at_head[-1]["state"] == "COMMENTED"
                and (at_head[-1].get("body") or "").startswith(DRAFT_PREFIX),
            }
            if at_head
            else None
        ),
        "orphan_id": body_at_head[-1]["id"] if body_at_head else None,
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
    reviews = github_cli.paginated(
        "api", "--paginate", f"repos/{repo}/pulls/{pr}/reviews"
    )
    return review_state(
        head_sha=head,
        bot=bot,
        substantive_ids=substantive_ids,
        force_push_times=force_push_times,
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
    """Return prior conversation plus every inline comment written by the bot."""
    repo = github_cli.repository()
    bot = str(github_cli.json_call("api", "user")["login"])
    pr_view = github_cli.json_call(
        "pr", "view", pr, "--repo", repo, "--json", "comments,reviews"
    )
    inline = [
        comment
        for thread in _review_threads(pr, repo, FEEDBACK_QUERY)
        for comment in thread["comments"]["nodes"]
        if github_cli.actor_login(comment.get("author")) == bot
    ]
    return {
        "previous_reviews": [
            {
                "state": review.get("state"),
                "submitted_at": review.get("submittedAt"),
                "body": review.get("body"),
            }
            for review in pr_view["reviews"]
            if github_cli.actor_login(review.get("author")) == bot
            and review.get("body")
        ],
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
            for comment in inline
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
