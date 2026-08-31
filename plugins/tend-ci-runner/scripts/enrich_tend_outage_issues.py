# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Add completed workflow failure annotations to open Tend outage issues."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import github_cli

LABEL = "tend-outage"
RUN_LINK = re.compile(r"/actions/runs/([0-9]+)")
ENRICHED_MARKER = re.compile(r"<!-- enriched-run:([0-9]+) -->")


def pending_run_ids(issue: dict[str, Any]) -> list[str]:
    """Return referenced run IDs that have no enrichment marker yet."""
    comments = issue.get("comments") or []
    referenced = {
        match
        for text in [issue.get("body") or "", *(c.get("body") or "" for c in comments)]
        for match in RUN_LINK.findall(text)
    }
    enriched = {
        match
        for comment in comments
        for match in ENRICHED_MARKER.findall(comment.get("body") or "")
    }
    return sorted(referenced - enriched)


def failure_details(repo: str, run_id: str) -> list[tuple[str, str]]:
    """Return each failed job's useful failure annotations."""
    try:
        response = github_cli.json_call(
            "api", f"repos/{repo}/actions/runs/{run_id}/jobs", quiet=True
        )
    except (subprocess.CalledProcessError, ValueError):
        return []

    details: list[tuple[str, str]] = []
    for job in response.get("jobs", []):
        if job.get("conclusion") != "failure":
            continue
        try:
            annotations = github_cli.json_call(
                "api", f"repos/{repo}/check-runs/{job['id']}/annotations", quiet=True
            )
        except (subprocess.CalledProcessError, ValueError):
            continue
        messages = [
            annotation.get("message") or ""
            for annotation in annotations
            if annotation.get("annotation_level") == "failure"
            and not (annotation.get("message") or "").startswith("Process completed")
        ]
        messages = [message for message in messages if message]
        if messages:
            details.append((job["name"], "\n\n".join(messages)))
    return details


def render_run(repo: str, run_id: str, details: list[tuple[str, str]]) -> str:
    """Render one run's section, including its durable dedup marker."""
    lines = [
        f"### [Run {run_id}](https://github.com/{repo}/actions/runs/{run_id})",
        "",
    ]
    if details:
        for job_name, message in details:
            lines.extend([f"#### {job_name}", "", "```", message, "```", ""])
    else:
        lines.extend(["No failure details could be extracted.", ""])
    lines.extend([f"<!-- enriched-run:{run_id} -->", ""])
    return "\n".join(lines)


def main() -> int:
    repo = github_cli.repository()
    issues = github_cli.json_call(
        "issue", "list", "--label", LABEL, "--state", "open", "--json", "number"
    )
    for row in issues:
        issue_number = str(row["number"])
        issue = github_cli.json_call(
            "issue", "view", issue_number, "--repo", repo, "--json", "body,comments"
        )
        sections = [
            render_run(repo, run_id, failure_details(repo, run_id))
            for run_id in pending_run_ids(issue)
        ]
        if not sections:
            continue
        with tempfile.TemporaryDirectory() as directory:
            body_file = Path(directory) / "enrichment.md"
            body_file.write_text("\n".join(sections), encoding="utf-8")
            github_cli.run(
                "issue",
                "comment",
                issue_number,
                "--repo",
                repo,
                "-F",
                str(body_file),
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
