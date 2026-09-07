"""Behavior tests for outage enrichment and its public-comment dedup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests import GH_PREAMBLE, fake_bin, tool_path, uv_script

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "tend-ci-runner"
    / "scripts"
    / "enrich_tend_outage_issues.py"
)
RUN_ID = "11"
JOB_ID = "101"
EXIT_ONLY = [
    {"annotation_level": "failure", "message": "Process completed with exit code 1."}
]


def _log(*lines: str, job: str = "test", step: str = "Nightly suite") -> str:
    return "".join(
        f"{job}\t{step}\t2026-09-06T08:0{i}:00.1234567Z {line}\n"
        for i, line in enumerate(lines)
    )


PYTEST_TAIL = _log(
    "FAILED tests/test_render_navigation.py::test_holding_a_key_repeats - AssertionError",
    "Actual value: 0",
    "==== 1 failed, 2021 passed, 6 skipped in 2053.74s (0:34:13) ====",
    "##[error]Process completed with exit code 1.",
)
COLOURED_TAIL = _log(
    "shellcheck.................................................^[[42mPassed^[[m",
    "^[[31mFAILED^[[0m tests/test_sandbox.py::test_proxy_env",
    "##[error]Process completed with exit code 1.",
)
BOM_TAIL = (
    "test\tRun the action\t\ufeff2026-09-06T08:00:00.1234567Z "
    "curl: (35) Recv failure\n" + _log("##[error]Process completed with exit code 35.")
)

FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$*" in
  "repo view"*) emit '{"nameWithOwner":"owner/repo"}' ;;
  "issue list"*) emit '[{"number":7}]' ;;
  "issue view"*) emit "$(cat "$ISSUE_JSON")" ;;
  *"/jobs"*) emit "$(cat "$JOBS_JSON")" ;;
  *"/annotations"*) emit "$(cat "$ANNOTATIONS_JSON")" ;;
  "run view"*--log-failed*)
    if [ -n "${LOG_FAILS:-}" ]; then exit 1; fi
    cat "$LOG_TXT"
    ;;
  "issue comment"*)
    prev=""
    for arg in "$@"; do
      [ "$prev" = "-F" ] && cp "$arg" "$POSTED_BODY"
      prev="$arg"
    done
    ;;
  *) exit 1 ;;
esac
"""
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bindir = fake_bin(tmp_path, gh=FAKE_GH)
    issue = {
        "body": f"Failed https://github.com/owner/repo/actions/runs/{RUN_ID}",
        "comments": [
            {
                "body": "Old https://github.com/owner/repo/actions/runs/10\n"
                "<!-- enriched-run:10 -->"
            }
        ],
    }
    jobs = {"jobs": [{"id": int(JOB_ID), "name": "tests", "conclusion": "failure"}]}
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
    log = tmp_path / "log.txt"
    log.write_text(PYTEST_TAIL)
    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "ISSUE_JSON": str(tmp_path / "issue.json"),
        "JOBS_JSON": str(tmp_path / "jobs.json"),
        "ANNOTATIONS_JSON": str(tmp_path / "annotations.json"),
        "POSTED_BODY": str(tmp_path / "posted.md"),
        "LOG_TXT": str(log),
    }


def _run(env: dict[str, str]) -> str:
    result = subprocess.run(
        uv_script(SCRIPT), env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    posted = Path(env["POSTED_BODY"])
    return posted.read_text() if posted.exists() else ""


def test_posts_one_batch_for_only_unenriched_runs(env: dict[str, str]) -> None:
    body = _run(env)

    assert body == (
        "### [Run 11](https://github.com/owner/repo/actions/runs/11)\n\n"
        "#### tests\n\n```\nassertion failed\n```\n\n"
        "<!-- enriched-run:11 -->\n"
    )
    calls = Path(env["GH_CALLS"]).read_text()
    assert "/actions/runs/10/jobs" not in calls


def test_a_plain_non_zero_exit_is_enriched_from_the_log(
    env: dict[str, str],
) -> None:
    Path(env["ANNOTATIONS_JSON"]).write_text(json.dumps(EXIT_ONLY))

    body = _run(env)

    assert "test_holding_a_key_repeats" in body
    assert "1 failed, 2021 passed" in body
    assert "No failure details could be extracted." not in body
    assert f"<!-- enriched-run:{RUN_ID} -->" in body


def test_the_log_prefix_is_stripped(env: dict[str, str]) -> None:
    Path(env["ANNOTATIONS_JSON"]).write_text(json.dumps(EXIT_ONLY))

    body = _run(env)

    assert "FAILED tests/test_render_navigation.py" in body
    assert "2026-09-06T08:00:00.1234567Z" not in body


def test_annotations_win_and_the_log_is_not_fetched(env: dict[str, str]) -> None:
    body = _run(env)

    assert "assertion failed" in body
    assert "log tail" not in body
    assert "run view" not in Path(env["GH_CALLS"]).read_text()


def test_annotation_fence_outgrows_agent_markdown(env: dict[str, str]) -> None:
    message = (
        "The failing command was:\n\n`````markdown\n```bash\nuv run pytest\n```\n"
        "`````\n\nRe-run it locally."
    )
    Path(env["ANNOTATIONS_JSON"]).write_text(
        json.dumps([{"annotation_level": "failure", "message": message}])
    )

    body = _run(env)

    assert f"#### tests\n\n``````\n{message}\n``````" in body
    assert body.index("Re-run it locally.") < body.index(
        f"<!-- enriched-run:{RUN_ID} -->"
    )


def test_a_run_with_no_recoverable_detail_still_records_the_marker(
    env: dict[str, str],
) -> None:
    Path(env["ANNOTATIONS_JSON"]).write_text(json.dumps(EXIT_ONLY))
    Path(env["LOG_TXT"]).write_text("")

    body = _run(env)

    assert "No failure details could be extracted." in body
    assert f"<!-- enriched-run:{RUN_ID} -->" in body


def test_a_rerun_that_went_green_is_enriched_from_the_failed_attempt(
    env: dict[str, str],
) -> None:
    # The row is recorded when the run fails; a rerun that then succeeds leaves
    # the default `latest` job view with nothing failed and no failed log, so
    # the only copy of the diagnosis lives on the earlier attempt.
    Path(env["JOBS_JSON"]).write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": int(JOB_ID),
                        "name": "review",
                        "conclusion": "failure",
                        "run_attempt": 1,
                    },
                    {
                        "id": 102,
                        "name": "review",
                        "conclusion": "success",
                        "run_attempt": 2,
                    },
                ]
            }
        )
    )
    Path(env["LOG_TXT"]).write_text("")

    body = _run(env)

    assert "filter=all" in Path(env["GH_CALLS"]).read_text()
    assert "#### review (attempt 1)\n\n```\nassertion failed\n```" in body
    assert "No failure details could be extracted." not in body


def test_a_single_attempt_job_heading_carries_no_attempt(env: dict[str, str]) -> None:
    body = _run(env)

    assert "#### tests\n\n" in body
    assert "attempt" not in body


def test_an_unavailable_log_does_not_abort_the_batch(env: dict[str, str]) -> None:
    Path(env["ANNOTATIONS_JSON"]).write_text(json.dumps(EXIT_ONLY))
    env["LOG_FAILS"] = "1"

    body = _run(env)

    assert "No failure details could be extracted." in body


def test_terminal_colour_escapes_are_stripped(env: dict[str, str]) -> None:
    Path(env["ANNOTATIONS_JSON"]).write_text(json.dumps(EXIT_ONLY))
    Path(env["LOG_TXT"]).write_text(COLOURED_TAIL)

    body = _run(env)

    assert "^[" not in body
    assert "shellcheck.................................................Passed" in body
    assert "FAILED tests/test_sandbox.py::test_proxy_env" in body


def test_log_fence_outgrows_fenced_output(env: dict[str, str]) -> None:
    Path(env["ANNOTATIONS_JSON"]).write_text(json.dumps(EXIT_ONLY))
    Path(env["LOG_TXT"]).write_text(
        _log(
            "````",
            "diagnostic",
            "````",
            "##[error]Process completed with exit code 1.",
        )
    )

    body = _run(env)

    assert "#### log tail\n\n`````\n````\ndiagnostic\n````\n" in body
    assert f"<!-- enriched-run:{RUN_ID} -->" in body


def test_the_step_boundary_bom_does_not_defeat_the_timestamp_strip(
    env: dict[str, str],
) -> None:
    Path(env["ANNOTATIONS_JSON"]).write_text(json.dumps(EXIT_ONLY))
    Path(env["LOG_TXT"]).write_text(BOM_TAIL)

    body = _run(env)

    assert "curl: (35) Recv failure" in body
    assert "2026-09-06T08:00:00.1234567Z" not in body
    assert "\ufeff" not in body


def _fence_lines(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("```")]


def _many_runs(env: dict[str, str], count: int) -> list[str]:
    runs = [str(int(RUN_ID) + i) for i in range(count)]
    Path(env["ISSUE_JSON"]).write_text(
        json.dumps(
            {
                "body": "\n".join(
                    f"https://github.com/owner/repo/actions/runs/{run}" for run in runs
                ),
                "comments": [],
            }
        )
    )
    return runs


def test_an_oversized_batch_is_truncated_between_runs(env: dict[str, str]) -> None:
    runs = _many_runs(env, 6)
    Path(env["ANNOTATIONS_JSON"]).write_text(json.dumps(EXIT_ONLY))
    Path(env["LOG_TXT"]).write_text(
        _log(*(["x" * 600] * 30), "##[error]Process completed with exit code 1.")
    )

    body = _run(env)

    assert len(body.encode()) < 65_536
    assert "_Truncated" in body
    assert f"<!-- enriched-run:{runs[0]} -->" in body
    assert f"<!-- enriched-run:{runs[-1]} -->" not in body
    assert len(_fence_lines(body)) % 2 == 0
    for run in runs:
        if f"### [Run {run}]" in body:
            assert f"<!-- enriched-run:{run} -->" in body
    assert body.rstrip().endswith(
        "_Truncated; the remaining runs are enriched by a later batch._"
    )


def test_one_huge_annotation_cannot_fill_the_body(env: dict[str, str]) -> None:
    Path(env["ANNOTATIONS_JSON"]).write_text(
        json.dumps(
            [
                {
                    "annotation_level": "failure",
                    "message": "E501 line too long " * 4000,
                },
                {
                    "annotation_level": "failure",
                    "message": "\n".join(["F401"] * 4000),
                },
            ]
        )
    )

    body = _run(env)

    assert len(body.encode()) < 65_536
    assert f"<!-- enriched-run:{RUN_ID} -->" in body
    assert "E501 line too long" in body
    assert len(_fence_lines(body)) % 2 == 0


def test_a_matrix_of_failed_jobs_cannot_fill_the_body(env: dict[str, str]) -> None:
    Path(env["JOBS_JSON"]).write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": int(JOB_ID) + i,
                        "name": f"test ({i})",
                        "conclusion": "failure",
                    }
                    for i in range(40)
                ]
            }
        )
    )
    Path(env["ANNOTATIONS_JSON"]).write_text(
        json.dumps(
            [
                {
                    "annotation_level": "failure",
                    "message": "\n".join(["y" * 600] * 30),
                }
            ]
        )
    )

    body = _run(env)

    assert len(body.encode()) < 65_536
    assert f"<!-- enriched-run:{RUN_ID} -->" in body
    assert "_Remaining failed jobs omitted._" in body
    assert len(_fence_lines(body)) % 2 == 0


def test_an_issue_with_nothing_new_posts_nothing(env: dict[str, str]) -> None:
    Path(env["ISSUE_JSON"]).write_text(
        json.dumps(
            {
                "body": f"https://github.com/owner/repo/actions/runs/{RUN_ID}",
                "comments": [{"body": f"<!-- enriched-run:{RUN_ID} -->"}],
            }
        )
    )

    assert _run(env) == ""
