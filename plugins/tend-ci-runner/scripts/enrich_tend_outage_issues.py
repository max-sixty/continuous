# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Add completed workflow failure details to open Tend outage issues.

Annotations carry precise agent failures. Ordinary non-zero exits only produce
a generic annotation, so their useful diagnosis comes from the failed log. The
rendered sections and batches are bounded at semantic boundaries so GitHub can
always accept the comment and a later nightly run can continue where this one
stopped.
"""

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
LITERAL_ANSI_ESCAPE = re.compile(r"\^\[\[[0-9;]*[A-Za-z]")
TIMESTAMP = re.compile(r"^[0-9T:.Z-]*Z ")
MAX_LINE_BYTES = 500
MAX_MESSAGE_LINES = 30
MAX_RUN_BYTES = 15_000
MAX_BATCH_BYTES = 30_000
TRUNCATED_BATCH = "_Truncated; the remaining runs are enriched by a later batch._"
OMITTED_JOBS = "_Remaining failed jobs omitted._"


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


def truncate_line(line: str) -> str:
    """Bound one rendered line without splitting a UTF-8 code point."""
    return line.encode()[:MAX_LINE_BYTES].decode(errors="ignore")


def fenced(body: str) -> str:
    """Wrap body in a fence longer than any backtick run it contains."""
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{body}\n{fence}"


def bounded_message(messages: list[str]) -> str:
    """Join useful annotations and bound their rendered lines."""
    return "\n".join(
        truncate_line(line)
        for line in "\n\n".join(messages).splitlines()[:MAX_MESSAGE_LINES]
    )


def annotation_details(repo: str, run_id: str) -> list[str]:
    """Render bounded failure annotations for one run."""
    try:
        response = github_cli.json_call(
            "api", f"repos/{repo}/actions/runs/{run_id}/jobs", quiet=True
        )
    except (subprocess.CalledProcessError, ValueError):
        return []

    details: list[str] = []
    rendered_bytes = 0
    for job in response.get("jobs", []):
        if job.get("conclusion") != "failure":
            continue
        if rendered_bytes > MAX_RUN_BYTES:
            details.append(OMITTED_JOBS)
            break
        try:
            annotations = github_cli.json_call(
                "api", f"repos/{repo}/check-runs/{job['id']}/annotations", quiet=True
            )
        except (subprocess.CalledProcessError, ValueError):
            continue
        messages = [
            str(annotation.get("message") or "")
            for annotation in annotations
            if annotation.get("annotation_level") == "failure"
            and not (annotation.get("message") or "").startswith("Process completed")
        ]
        messages = [message for message in messages if message]
        if messages:
            detail = f"#### {job['name']}\n\n{fenced(bounded_message(messages))}"
            details.append(detail)
            rendered_bytes += len(detail.encode())
    return details


def clean_log_line(line: str) -> str:
    """Remove GitHub's log transport prefix and literal terminal escapes."""
    fields = line.split("\t", 2)
    if len(fields) == 3:
        line = fields[2]
    line = line.removeprefix("\ufeff")
    line = TIMESTAMP.sub("", line, count=1)
    return truncate_line(LITERAL_ANSI_ESCAPE.sub("", line))


def log_details(repo: str, run_id: str) -> list[str]:
    """Render the tail ending at the run's last error, when available."""
    try:
        output = github_cli.run(
            "run", "view", run_id, "--repo", repo, "--log-failed", quiet=True
        )
    except subprocess.CalledProcessError:
        return []

    lines = output.splitlines()
    errors = [index for index, line in enumerate(lines) if "##[error]" in line]
    if not errors:
        return []
    end = errors[-1]
    window = lines[max(0, end - 30) : end + 1]
    body = "\n".join(clean_log_line(line) for line in window)
    return [f"#### log tail\n\n{fenced(body)}"]


def failure_details(repo: str, run_id: str) -> list[str]:
    """Prefer precise annotations, falling back to the failed-log tail."""
    return annotation_details(repo, run_id) or log_details(repo, run_id)


def render_run(repo: str, run_id: str, details: list[str]) -> str:
    """Render one run's section, including its durable dedup marker."""
    lines = [
        f"### [Run {run_id}](https://github.com/{repo}/actions/runs/{run_id})",
        "",
    ]
    if details:
        for detail in details:
            lines.extend([detail, ""])
    else:
        lines.extend(["No failure details could be extracted.", ""])
    lines.extend([f"<!-- enriched-run:{run_id} -->", ""])
    return "\n".join(lines)


def render_batch(repo: str, run_ids: list[str]) -> str:
    """Render whole run sections until the safe per-comment threshold."""
    sections: list[str] = []
    rendered_bytes = 0
    for run_id in run_ids:
        if rendered_bytes > MAX_BATCH_BYTES:
            sections.append(TRUNCATED_BATCH + "\n")
            break
        section = render_run(repo, run_id, failure_details(repo, run_id))
        sections.append(section)
        rendered_bytes += len(section.encode())
    return "\n".join(sections)


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
        run_ids = pending_run_ids(issue)
        if not run_ids:
            continue
        body = render_batch(repo, run_ids)
        with tempfile.TemporaryDirectory() as directory:
            body_file = Path(directory) / "enrichment.md"
            body_file.write_text(body, encoding="utf-8")
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
