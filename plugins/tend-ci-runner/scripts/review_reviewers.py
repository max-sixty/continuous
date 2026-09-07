# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Own gist-backed evidence for the cross-repository reviewer survey."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import github_cli

LABEL = "review-reviewers-tracking"
TRACKING_BODY = """Monthly tracking issue for `review-reviewers`. Per-target evidence lives in secret gists owned by the bot. A comment below is posted when each target's gist is first created.

**Do not close manually** — a new issue is created each month.
"""


def _state_path() -> Path:
    return Path(
        os.environ.get("REVIEW_REVIEWERS_STATE", "/tmp/review-reviewers-state.json")
    )


def _findings_path() -> Path:
    return Path(os.environ.get("REVIEW_REVIEWERS_FINDINGS", "/tmp/findings.md"))


def _issue_number(url: str) -> int:
    return int(url.strip().rstrip("/").rsplit("/", 1)[-1])


def _gist_content(gist_id: str) -> str:
    return github_cli.run("gist", "view", gist_id, "-f", "findings.md", "--raw")


def _create_gist(target: str, month: str, description: str) -> dict[str, Any]:
    content = (
        f"# review-reviewers evidence — {target} — {month}\n\n"
        "Secret gist. Append-only log of below-threshold findings used for "
        "gate evaluation.\n"
    )
    response = github_cli.json_call(
        "api",
        "gists",
        "-X",
        "POST",
        "--input",
        "-",
        input=json.dumps(
            {
                "description": description,
                "public": False,
                "files": {"findings.md": {"content": content}},
            }
        ),
    )
    return response


def prepare(target: str, *, now: datetime | None = None) -> int:
    """Find/create this target's monthly gist and print both evidence windows."""
    now = now or datetime.now(UTC)
    month = now.strftime("%Y-%m")
    previous_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    issues = github_cli.json_call(
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        LABEL,
        "--limit",
        "100",
        "--json",
        "number,title",
    )
    matches = sorted(
        int(issue["number"])
        for issue in issues
        if month in str(issue.get("title") or "")
    )
    if matches:
        tracking_number = matches[0]
    else:
        tracking_number = _issue_number(
            github_cli.run(
                "issue",
                "create",
                "--title",
                f"{LABEL}: {month}",
                "--label",
                LABEL,
                "--body",
                TRACKING_BODY,
            )
        )

    gists = github_cli.paginated("api", "--paginate", "gists?per_page=100")
    description = f"review-reviewers evidence: {target} {month}"
    gist = next(
        (gist for gist in gists if gist.get("description") == description), None
    )
    if gist is None:
        gist = _create_gist(target, month, description)
        gist_url = str(gist["html_url"])
        github_cli.run(
            "api",
            f"repos/{github_cli.repository()}/issues/{tracking_number}/comments",
            "-X",
            "POST",
            "--input",
            "-",
            input=json.dumps({"body": f"Evidence gist for `{target}`: {gist_url}\n"}),
        )
    gist_id = str(gist["id"])
    gist_url = str(gist.get("html_url") or f"https://gist.github.com/{gist_id}")

    previous_description = f"review-reviewers evidence: {target} {previous_month}"
    previous = next(
        (gist for gist in gists if gist.get("description") == previous_description),
        None,
    )
    state = {
        "target": target,
        "month": month,
        "tracking_number": tracking_number,
        "gist_id": gist_id,
        "gist_url": gist_url,
    }
    _state_path().write_text(json.dumps(state, indent=2) + "\n")
    github_cli.dump(
        {
            **state,
            "current_evidence": _gist_content(gist_id),
            "previous_evidence": (
                _gist_content(str(previous["id"])) if previous is not None else ""
            ),
        }
    )
    return 0


def append() -> int:
    """Append this run's findings to the prepared target gist."""
    state = json.loads(_state_path().read_text())
    findings = _findings_path().read_text()
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not run_id or run_id not in findings:
        print(
            f"{_findings_path()} does not contain GITHUB_RUN_ID={run_id}; refusing to patch gist",
            file=sys.stderr,
        )
        return 2
    gist_id = str(state["gist_id"])
    combined = _gist_content(gist_id) + findings
    github_cli.run(
        "api",
        f"gists/{gist_id}",
        "-X",
        "PATCH",
        "--input",
        "-",
        input=json.dumps({"files": {"findings.md": {"content": combined}}}),
    )
    github_cli.dump({"gist_url": state["gist_url"], "action": "appended"})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "prepare-evidence" and "/" in args[1]:
        return prepare(args[1])
    if args == ["append-evidence"]:
        return append()
    print(
        f"usage: {sys.argv[0]} prepare-evidence <owner/repo> | append-evidence",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(2) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
