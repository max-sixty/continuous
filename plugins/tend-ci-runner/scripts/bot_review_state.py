# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Resolve which bot review, if any, anchors a pull request's current head."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import github_cli

DRAFT_PREFIX = "Reviewing as a draft —"


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
        decisions[-1] if decisions and decisions[-1].get("state") == "APPROVED" else None
    )

    return {
        "head_sha": head_sha,
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
        "standing_approval_id": (
            standing_approval["id"] if standing_approval else ""
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        print(f"usage: {sys.argv[0]} <pr-number>", file=sys.stderr)
        return 2
    pr = args[0]
    repo = github_cli.repository()
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
    github_cli.dump(
        review_state(
            head_sha=head,
            bot=bot,
            substantive_ids=substantive_ids,
            force_push_times=force_push_times,
            reviews=reviews,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
