# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Collect maintainer corrections made during a review-runs window."""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from urllib.parse import quote

import github_cli


def correction_report(
    *,
    since: str,
    bot: str,
    bot_prs: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the stable JSON shape consumed by the review-runs skill."""
    return {
        "since": since,
        "bot": bot,
        "dispositions": [
            pr for pr in bot_prs if pr.get("closedAt") and pr["closedAt"] > since
        ],
        "comments": [
            {
                "created": comment["created_at"],
                "updated": comment["updated_at"],
                "url": comment["html_url"],
                "body": (comment.get("body") or "")[:300],
            }
            for comment in comments
            if github_cli.actor_login(comment.get("user")) != bot
        ],
        "reviews": [
            {
                "at": review["submitted_at"],
                "state": review["state"],
                "url": review["html_url"],
                "body": review["body"][:300],
            }
            for review in reviews
            if github_cli.actor_login(review.get("user")) != bot
            and review.get("submitted_at", "") >= since
            and review.get("body")
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0]:
        print(
            f"usage: {sys.argv[0]} <since>   # RFC3339, e.g. "
            "$(cat /tmp/review-runs-since)",
            file=sys.stderr,
        )
        return 2
    since = args[0]
    repo = github_cli.repository()
    bot = str(github_cli.json_call("api", "user").get("login") or "")
    if not bot:
        print("could not resolve the bot login from `gh api user`", file=sys.stderr)
        return 2

    bot_prs = github_cli.json_call(
        "pr",
        "list",
        "--repo",
        repo,
        "--author",
        bot,
        "--state",
        "all",
        "--limit",
        "200",
        "--json",
        "number,title,state,closedAt",
    )
    encoded_since = quote(since, safe="-TZ")
    comments = [
        comment
        for endpoint in ("issues", "pulls")
        for comment in github_cli.paginated(
            "api",
            "--paginate",
            f"repos/{repo}/{endpoint}/comments?since={encoded_since}&per_page=100",
        )
    ]

    candidates = github_cli.json_call(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        "200",
        "--search",
        f"updated:>={since}",
        "--json",
        "number",
    )
    reviews = [
        review
        for candidate in candidates
        for review in github_cli.paginated(
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{candidate['number']}/reviews?per_page=100",
        )
    ]

    github_cli.dump(
        correction_report(
            since=since,
            bot=bot,
            bot_prs=bot_prs,
            comments=comments,
            reviews=reviews,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
