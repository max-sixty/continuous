# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Decide whether a mention event should start an agent session."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def gh(*args: str, quiet: bool = False) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        if result.stderr and not quiet:
            sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return result.stdout


def gh_json(*args: str, quiet: bool = False) -> Any:
    return json.loads(gh(*args, quiet=quiet))


def gh_paginated(path: str) -> list[dict[str, Any]]:
    text = gh("api", "--paginate", path)
    decoder = json.JSONDecoder()
    position = 0
    items: list[dict[str, Any]] = []
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        page, position = decoder.raw_decode(text, position)
        if not isinstance(page, list):
            raise TypeError("paginated GitHub response was not an array")
        items.extend(page)
    return items


def actor_login(actor: object) -> str:
    """Return a GitHub actor login, including for deleted-account records."""
    if not isinstance(actor, dict):
        return ""
    return str(actor.get("login") or "")


def output(name: str, value: str | bool) -> None:
    rendered = str(value).lower() if isinstance(value, bool) else value
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={rendered}\n")


def verdict(should_run: bool, reason: str = "") -> int:
    output("should_run", should_run)
    if reason:
        output("reason", reason)
    return 0


def main() -> int:
    env = os.environ
    bot = env.get("BOT_NAME", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    kind = env.get("EVENT_NAME", "")
    comment_body = env.get("COMMENT_BODY", "")
    comment_author = env.get("COMMENT_AUTHOR", "")
    review_author = ""
    review_state = ""
    inline: list[dict[str, Any]] = []
    fresh_inline = 0

    if kind == "repository_dispatch":
        kind = env.get("PAYLOAD_KIND", "")
        pr = env.get("PAYLOAD_PR", "")
        item_id = env.get("PAYLOAD_ID", "")
        if not pr.isdigit() or not item_id.isdigit():
            print("malformed dispatch payload — skipping")
            return verdict(False)
        if kind == "pull_request_review":
            try:
                review = gh_json(
                    "api", f"repos/{repo}/pulls/{pr}/reviews/{item_id}", quiet=True
                )
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                print(f"review {item_id} not found on PR {pr} — skipping")
                return verdict(False)
            review_author = actor_login(review.get("user"))
            review_state = str(review["state"]).lower()
            comment_body = review.get("body") or ""
            output("url", review["html_url"])
            output("ts", review.get("submitted_at") or "")
        elif kind == "pull_request_review_comment":
            try:
                comment = gh_json(
                    "api", f"repos/{repo}/pulls/comments/{item_id}", quiet=True
                )
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                print(f"comment {item_id} not found — skipping")
                return verdict(False)
            expected_pr = f"https://api.github.com/repos/{repo}/pulls/{pr}"
            if comment.get("pull_request_url") != expected_pr:
                print(f"comment {item_id} does not belong to PR {pr} — skipping")
                return verdict(False)
            comment_author = actor_login(comment.get("user"))
            comment_body = comment.get("body") or ""
            output("url", comment["html_url"])
            output("ts", comment["updated_at"])
        else:
            print(f"unknown dispatch kind '{kind}' — skipping")
            return verdict(False)

    if kind == "issues":
        return verdict(True)

    if (
        kind in {"issue_comment", "pull_request_review_comment"}
        and comment_author == bot
    ):
        return verdict(False)
    if comment_body and f"@{bot}" in comment_body:
        return verdict(True, "mention")
    if kind == "issue_comment" and env.get("COMMENT_AUTHOR_TYPE") == "Bot":
        return verdict(False)

    if kind == "pull_request_review":
        inline = gh_paginated(
            f"repos/{repo}/pulls/{env.get('PAYLOAD_PR', '')}/reviews/"
            f"{env.get('PAYLOAD_ID', '')}/comments"
        )
        if any(f"@{bot}" in (comment.get("body") or "") for comment in inline):
            return verdict(True, "mention")
        fresh_inline = sum(comment.get("in_reply_to_id") is None for comment in inline)
        if review_state == "approved" and not comment_body and not inline:
            return verdict(False)

    if kind == "issue_comment":
        issue_number = env.get("ISSUE_OR_PR_NUMBER", "")
        if not env.get("PR_URL"):
            if env.get("ISSUE_AUTHOR") == bot or f"@{bot}" in env.get("ISSUE_BODY", ""):
                return verdict(True)
            comments = gh_paginated(f"repos/{repo}/issues/{issue_number}/comments")
            return verdict(
                any(actor_login(comment.get("user")) == bot for comment in comments)
            )
        pr_number = issue_number
    else:
        pr_number = env.get("PAYLOAD_PR", "")

    pr = gh_json("pr", "view", pr_number, "--repo", repo, "--json", "author")
    pr_author = actor_login(pr.get("author"))
    if kind == "pull_request_review" and review_author == bot:
        if pr_author == bot and (comment_body or fresh_inline > 0):
            return verdict(True, "participation")
        return verdict(False)
    if pr_author == bot:
        return verdict(True, "participation")

    reviews = gh_paginated(f"repos/{repo}/pulls/{pr_number}/reviews")
    if any(actor_login(review.get("user")) == bot for review in reviews):
        return verdict(True, "participation")
    comments = gh_paginated(f"repos/{repo}/issues/{pr_number}/comments")
    if any(actor_login(comment.get("user")) == bot for comment in comments):
        return verdict(True, "participation")
    return verdict(False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode or 1) from None
