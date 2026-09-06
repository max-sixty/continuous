# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Decide whether the notifications workflow has work for an agent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

GRAPHQL_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    nodes { ... on PullRequest {
      mergeable headRefOid
      comments(last: 100) { nodes { author { login } body } }
    } }
  }
}
"""


def _gh(*args: str, quiet: bool = False) -> str:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )
    if result.returncode:
        if result.stderr and not quiet:
            sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return result.stdout


def _json(*args: str, quiet: bool = False) -> Any:
    return json.loads(_gh(*args, quiet=quiet))


def _paginated(path: str) -> list[Any]:
    text = _gh("api", path, "--paginate", quiet=True)
    decoder = json.JSONDecoder()
    pages: list[Any] = []
    position = 0
    saw_page = False
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        page, position = decoder.raw_decode(text, position)
        saw_page = True
        if not isinstance(page, list):
            raise TypeError("paginated GitHub response was not an array")
        pages.extend(page)
    if not saw_page:
        raise ValueError("paginated GitHub response was empty")
    return pages


def _output(name: str, value: str | int) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def _notifications(cutoff: str) -> int:
    try:
        return len(_paginated(f"notifications?before={cutoff}&per_page=100"))
    except (
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        TypeError,
        ValueError,
    ):
        print("::warning::notifications fetch failed; queue left for the next cycle")
        return 0


def _actor_login(actor: object) -> str:
    if not isinstance(actor, dict):
        return ""
    return str(actor.get("login") or "")


def _is_deferred(pr: dict[str, Any], bot: str) -> bool:
    marker = f"<!-- tend-conflict-deferred head={pr.get('headRefOid', '')} -->"
    comments = pr.get("comments")
    nodes = comments.get("nodes", []) if isinstance(comments, dict) else []
    return any(
        _actor_login(comment.get("author")) == bot
        and str(comment.get("body") or "").rstrip().split("\n")[-1] == marker
        for comment in nodes
        if isinstance(comment, dict)
    )


def _conflicts(repo: str) -> int:
    try:
        bot = _gh("api", "user", "--jq", ".login", quiet=True).strip()
        if not bot:
            raise ValueError("authenticated GitHub login was empty")
        response = _json(
            "api",
            "graphql",
            "-f",
            f"query={GRAPHQL_QUERY}",
            "-f",
            f"q=repo:{repo} author:{bot} is:pr is:open",
            quiet=True,
        )
        nodes = response["data"]["search"]["nodes"]
        if not isinstance(nodes, list):
            raise TypeError("GraphQL search nodes were not an array")
        return sum(
            pr.get("mergeable") != "MERGEABLE" and not _is_deferred(pr, bot)
            for pr in nodes
            if isinstance(pr, dict)
        )
    except (
        json.JSONDecodeError,
        KeyError,
        subprocess.CalledProcessError,
        TypeError,
        ValueError,
    ):
        print("::warning::bot PR conflict scan failed; retrying next cycle")
        return 0


def main(*, now: datetime | None = None) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    cutoff = ((now or datetime.now(UTC)) - timedelta(minutes=10)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _output("cutoff", cutoff)

    try:
        _gh(
            "api",
            f"repos/{repo}/subscription",
            "-X",
            "PUT",
            "-F",
            "subscribed=true",
            "-F",
            "ignored=false",
            "--silent",
            quiet=True,
        )
    except subprocess.CalledProcessError:
        print("::warning::could not enable repository watching; retrying next cycle")

    count = _notifications(cutoff)
    _output("count", count)
    conflict_count = _conflicts(repo)
    _output("conflict_count", conflict_count)

    if count == 0 and conflict_count == 0:
        print("No notification or conflict work — skipping")
    else:
        if count:
            print(f"{count} notification task(s) — proceeding")
        if conflict_count:
            print(f"{conflict_count} possible conflicted bot PR(s) — proceeding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
