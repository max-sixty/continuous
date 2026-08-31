"""Behavior tests for outage enrichment and its public-comment dedup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests import GH_PREAMBLE, fake_bin, tool_path, uv_script

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "tend-ci-runner"
    / "scripts"
    / "enrich_tend_outage_issues.py"
)

FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$1 $2" in
  "repo view") emit '{"nameWithOwner":"owner/repo"}' ;;
  "issue list") emit '[{"number":7}]' ;;
  "issue view") emit "$(cat "$ISSUE_JSON")" ;;
  "issue comment")
    prev=""
    for arg in "$@"; do
      [ "$prev" = "-F" ] && cp "$arg" "$POSTED_BODY"
      prev="$arg"
    done
    ;;
  api*)
    case "$2" in
      */actions/runs/11/jobs) emit "$(cat "$JOBS_JSON")" ;;
      */check-runs/101/annotations) emit "$(cat "$ANNOTATIONS_JSON")" ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
"""
)


def test_posts_one_batch_for_only_unenriched_runs(tmp_path: Path) -> None:
    bindir = fake_bin(tmp_path, gh=FAKE_GH)
    issue = {
        "body": "Failed https://github.com/owner/repo/actions/runs/11",
        "comments": [
            {
                "body": "Old https://github.com/owner/repo/actions/runs/10\n"
                "<!-- enriched-run:10 -->"
            }
        ],
    }
    jobs = {"jobs": [{"id": 101, "name": "tests", "conclusion": "failure"}]}
    annotations = [
        {"annotation_level": "failure", "message": "assertion failed"},
        {"annotation_level": "failure", "message": "Process completed with exit 1"},
    ]
    for name, value in (
        ("issue.json", issue),
        ("jobs.json", jobs),
        ("annotations.json", annotations),
    ):
        (tmp_path / name).write_text(json.dumps(value))
    env = {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "ISSUE_JSON": str(tmp_path / "issue.json"),
        "JOBS_JSON": str(tmp_path / "jobs.json"),
        "ANNOTATIONS_JSON": str(tmp_path / "annotations.json"),
        "POSTED_BODY": str(tmp_path / "posted.md"),
    }

    result = subprocess.run(
        uv_script(SCRIPT), env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "posted.md").read_text() == (
        "### [Run 11](https://github.com/owner/repo/actions/runs/11)\n\n"
        "#### tests\n\n```\nassertion failed\n```\n\n"
        "<!-- enriched-run:11 -->\n"
    )
    calls = (tmp_path / "gh-calls.log").read_text()
    assert "/actions/runs/10/jobs" not in calls
