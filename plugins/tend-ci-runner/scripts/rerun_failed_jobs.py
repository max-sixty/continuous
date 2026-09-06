# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Rerun one workflow run's failed jobs and report the new conclusions."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import github_cli


def _run_attempt(path: str) -> int:
    return int(github_cli.json_call("api", path)["run_attempt"])


def _job(path: str, job_id: int) -> dict[str, Any]:
    return github_cli.json_call("api", f"{path.rsplit('/runs/', 1)[0]}/jobs/{job_id}")


def main(
    argv: list[str] | None = None, *, sleep: Callable[[float], None] = time.sleep
) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        print(f"usage: {sys.argv[0]} <run-id>", file=sys.stderr)
        return 2
    run_id = args[0]
    repo = os.environ["GITHUB_REPOSITORY"]
    run_api = f"repos/{repo}/actions/runs/{run_id}"
    base_attempt = _run_attempt(run_api)
    github_cli.run("run", "rerun", run_id, "--failed", "--repo", repo)

    attempt = base_attempt
    for _ in range(6):
        sleep(5)
        attempt = _run_attempt(run_api)
        if attempt > base_attempt:
            break
    if attempt <= base_attempt:
        print(
            f"no new attempt surfaced after the rerun (still attempt {attempt}) "
            "— the rerun did not take; UNVERIFIED"
        )
        return 1

    response = github_cli.json_call("api", f"{run_api}/jobs?filter=latest")
    job_ids = [
        int(job["id"]) for job in response["jobs"] if int(job["run_attempt"]) == attempt
    ]
    if not job_ids:
        print(f"attempt {attempt} exists but lists no jobs yet — UNVERIFIED")
        return 1

    for _ in range(9):
        jobs = [_job(run_api, job_id) for job_id in job_ids]
        if all(job["status"] == "completed" for job in jobs):
            for job in jobs:
                print(f"{job.get('conclusion') or ''}\t{job['name']}")
            return 0
        sleep(60)
    print("Rerun jobs still running after 9 minutes — UNVERIFIED")
    return 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
