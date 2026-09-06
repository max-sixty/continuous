# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Own the monthly evidence lifecycle for the review-runs workflow."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import github_cli

LABEL = "review-runs-tracking"
BODY = """Monthly tracking issue for below-threshold findings. Each run appends findings as a comment. Future runs read these to build cumulative evidence.

**Do not close manually** — a new issue is created each month, and prior months are closed automatically.
"""
EVIDENCE_HEADING = re.compile(r"(^|\n)## Run [0-9]")


def _state_path() -> Path:
    return Path(os.environ.get("REVIEW_RUNS_STATE", "/tmp/review-runs-state.json"))


def _findings_path() -> Path:
    return Path(os.environ.get("REVIEW_RUNS_FINDINGS", "/tmp/findings.md"))


def _issue_number(url: str) -> int:
    return int(url.strip().rstrip("/").rsplit("/", 1)[-1])


def _comments(number: int) -> list[dict[str, Any]]:
    response = github_cli.json_call("issue", "view", str(number), "--json", "comments")
    return [
        {
            "author": github_cli.actor_login(comment.get("author")),
            "body": comment.get("body"),
        }
        for comment in response["comments"]
    ]


def prepare(*, now: datetime | None = None) -> int:
    """Find/create this month's tracker, close stale ones, and show history."""
    now = now or datetime.now(UTC)
    month = now.strftime("%Y-%m")
    previous_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    issues = github_cli.json_call(
        "issue",
        "list",
        "--state",
        "all",
        "--label",
        LABEL,
        "--limit",
        "100",
        "--json",
        "number,title,state",
    )
    current = sorted(
        int(issue["number"])
        for issue in issues
        if issue.get("state") == "OPEN" and month in str(issue.get("title") or "")
    )
    if current:
        number = current[0]
    else:
        url = github_cli.run(
            "issue",
            "create",
            "--title",
            f"{LABEL}: {month}",
            "--label",
            LABEL,
            "--body",
            BODY,
        )
        number = _issue_number(url)

    stale = sorted(
        int(issue["number"])
        for issue in issues
        if issue.get("state") == "OPEN" and month not in str(issue.get("title") or "")
    )
    for old in stale:
        github_cli.run(
            "issue",
            "close",
            str(old),
            "--comment",
            f"Superseded by #{number} ({month}).",
        )

    previous = sorted(
        int(issue["number"])
        for issue in issues
        if previous_month in str(issue.get("title") or "")
    )
    state = {"tracking_number": number, "month": month}
    _state_path().write_text(json.dumps(state, indent=2) + "\n")
    github_cli.dump(
        {
            **state,
            "current_comments": _comments(number),
            "previous_comments": _comments(previous[0]) if previous else [],
        }
    )
    return 0


def _post_comment(repo: str, number: int, body: str) -> None:
    github_cli.run(
        "api",
        f"repos/{repo}/issues/{number}/comments",
        "-X",
        "POST",
        "--input",
        "-",
        input=json.dumps({"body": body}),
    )


def append() -> int:
    """Append this run's findings to its current monthly evidence comment."""
    state = json.loads(_state_path().read_text())
    number = int(state["tracking_number"])
    findings = _findings_path().read_text()
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not run_id or run_id not in findings:
        print(
            f"{_findings_path()} does not contain GITHUB_RUN_ID={run_id}; refusing to post",
            file=sys.stderr,
        )
        return 2

    repo = github_cli.repository()
    bot = str(github_cli.json_call("api", "user")["login"])
    comments = github_cli.paginated(
        "api", "--paginate", f"repos/{repo}/issues/{number}/comments?per_page=100"
    )
    existing = [
        comment
        for comment in comments
        if github_cli.actor_login(comment.get("user")) == bot
        and EVIDENCE_HEADING.search(str(comment.get("body") or ""))
    ]
    if not existing:
        _post_comment(repo, number, findings)
        github_cli.dump({"tracking_number": number, "action": "created"})
        return 0

    prior = str(existing[-1].get("body") or "")
    combined = prior + findings
    if len(combined.encode()) >= 60_000:
        _post_comment(repo, number, findings)
        action = "created"
    else:
        github_cli.run(
            "api",
            f"repos/{repo}/issues/comments/{existing[-1]['id']}",
            "-X",
            "PATCH",
            "--input",
            "-",
            input=json.dumps({"body": combined}),
        )
        action = "appended"
    github_cli.dump({"tracking_number": number, "action": action})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["prepare-evidence"]:
        return prepare()
    if args == ["append-evidence"]:
        return append()
    print(f"usage: {sys.argv[0]} prepare-evidence|append-evidence", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(2) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
