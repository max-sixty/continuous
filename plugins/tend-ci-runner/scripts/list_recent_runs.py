# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""List recently completed Tend workflow runs without leaving coverage gaps."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import github_cli

WINDOW_CAP = timedelta(hours=6)
REVIEW_RUNS_WINDOW_CAP = timedelta(hours=49)
REVIEW_RUNS_DEFAULT_WINDOW = timedelta(hours=25)
AD_HOC_WINDOW = timedelta(hours=1)
CREATION_CUSHION = timedelta(hours=2)
REVIEW_RUNS_CREATION_CUSHION = timedelta(hours=24)
RUN_LIMIT = 200


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    profile = args[0] if args else ""
    if profile not in {"review-reviewers", "review-runs"}:
        print(
            f"usage: {sys.argv[0]} review-reviewers|review-runs [workflow-prefix ...]",
            file=sys.stderr,
        )
        return 2
    prefixes = args[1:] or ["tend-"]
    now = now or datetime.now(UTC)
    repo_args = (
        ["-R", os.environ["TARGET_REPO"]] if os.environ.get("TARGET_REPO") else []
    )

    workflow_rows = github_cli.json_call(
        "workflow", "list", *repo_args, "--json", "name"
    )
    workflows = github_cli.unique(
        row["name"]
        for prefix in prefixes
        for row in workflow_rows
        if row["name"].startswith(prefix)
    )

    window_cap = REVIEW_RUNS_WINDOW_CAP if profile == "review-runs" else WINDOW_CAP
    floor_cap = now - window_cap
    current_workflow = os.environ.get("GITHUB_WORKFLOW")
    if current_workflow:
        anchors = github_cli.json_call(
            "run",
            "list",
            "--repo",
            os.environ["GITHUB_REPOSITORY"],
            "--workflow",
            current_workflow,
            "--status",
            "success",
            "--limit",
            "5",
            "--json",
            "databaseId,createdAt",
        )
        current_run = int(os.environ.get("GITHUB_RUN_ID", "0"))
        previous = next(
            (row for row in anchors if int(row["databaseId"]) != current_run), None
        )
        if previous is None:
            completed_after = (
                now - REVIEW_RUNS_DEFAULT_WINDOW
                if profile == "review-runs"
                else floor_cap
            )
            print(
                f"WARNING: no successful '{current_workflow}' run found. Window "
                f"floored at {_stamp(completed_after)}; anything earlier is NOT in this "
                "list. Record a coverage gap, not an all-clear.",
                file=sys.stderr,
            )
        else:
            completed_after = _parse_time(previous["createdAt"])
            if completed_after < floor_cap:
                print(
                    f"WARNING: the last successful '{current_workflow}' run started "
                    f"{previous['createdAt']}, more than "
                    f"{window_cap.total_seconds() / 3600:g}h back. Window floored at "
                    f"{_stamp(floor_cap)}; runs that completed before it are NOT in "
                    "this list. Record a coverage gap, not an all-clear.",
                    file=sys.stderr,
                )
                completed_after = floor_cap
    else:
        completed_after = now - AD_HOC_WINDOW

    cushion = (
        REVIEW_RUNS_CREATION_CUSHION if profile == "review-runs" else CREATION_CUSHION
    )
    created_since = (completed_after - cushion).strftime("%Y-%m-%dT%H:%M:%S")
    if profile == "review-runs":
        Path(
            os.environ.get("REVIEW_RUNS_SINCE_FILE", "/tmp/review-runs-since")
        ).write_text(f"{_stamp(completed_after)}\n")
    runs_by_id: dict[int, dict[str, Any]] = {}
    for workflow in workflows:
        rows = github_cli.json_call(
            "run",
            "list",
            *repo_args,
            "--workflow",
            workflow,
            "--created",
            f">={created_since}",
            "--json",
            "databaseId,conclusion,createdAt,updatedAt,name",
            "--limit",
            str(RUN_LIMIT),
        )
        if len(rows) >= RUN_LIMIT:
            print(
                f"WARNING: '{workflow}' returned {RUN_LIMIT} runs, the fetch limit "
                "— older runs in this window are likely missing from the list. "
                "Record a coverage gap, not an all-clear.",
                file=sys.stderr,
            )
        for row in rows:
            conclusion = row.get("conclusion")
            if conclusion and _parse_time(row["updatedAt"]) >= completed_after:
                runs_by_id[int(row["databaseId"])] = row

    json.dump(list(runs_by_id.values()), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
