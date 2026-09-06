"""Tests for plugins/tend-ci-runner/scripts/enrich-tend-outage-issues.sh.

The script turns a bare run link in a `tend-outage` issue into a readable
failure, then marks the run `<!-- enriched-run:ID -->` so no later night
retries it. That marker makes an empty enrichment permanent, so the cases
that matter are the ones where detail exists somewhere: annotations carry it
for an agent failure, and only the job log carries it for a job that fails by
a plain non-zero exit — the shape of every test suite.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "tend-ci-runner"
    / "scripts"
    / "enrich-tend-outage-issues.sh"
)

RUN_ID = "33981618265"
JOB_ID = "101349874026"

# The lone annotation a job that just exits non-zero produces. The script
# filters it out by design — it names no cause.
EXIT_ONLY = [
    {"annotation_level": "failure", "message": "Process completed with exit code 1."}
]


# `gh run view --log-failed` prefixes every line with `job<TAB>step<TAB>` and a
# timestamp.
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

FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$*" in
  "repo view"*)       emit '{"nameWithOwner":"owner/repo"}' ;;
  "issue list"*)      emit '[{"number":305}]' ;;
  "issue view"*)      emit "$(cat "$ISSUE_JSON")" ;;
  *"/jobs"*)          emit "$(cat "$JOBS_JSON")" ;;
  *"/annotations"*)   emit "$(cat "$ANNOTATIONS_JSON")" ;;
  "run view"*--log-failed*)
    if [ -n "${LOG_FAILS:-}" ]; then exit 1; fi
    cat "$LOG_TXT" ;;
  "issue comment"*)
    prev=""
    for arg in "$@"; do
      [ "$prev" = "-F" ] && cp "$arg" "$COMMENT_OUT"
      prev="$arg"
    done ;;
  *) exit 1 ;;
esac
"""
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """One open tracker referencing one unenriched run with one failed job."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH)
    issue = {
        "body": f"https://github.com/owner/repo/actions/runs/{RUN_ID}",
        "comments": [],
    }
    paths = {
        "ISSUE_JSON": json.dumps(issue),
        "JOBS_JSON": json.dumps(
            {"jobs": [{"id": int(JOB_ID), "name": "test", "conclusion": "failure"}]}
        ),
        "ANNOTATIONS_JSON": json.dumps(EXIT_ONLY),
    }
    written = {}
    for key, value in paths.items():
        path = tmp_path / f"{key.lower()}.json"
        path.write_text(value)
        written[key] = str(path)
    log = tmp_path / "log.txt"
    log.write_text(PYTEST_TAIL)
    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "COMMENT_OUT": str(tmp_path / "comment.md"),
        "LOG_TXT": str(log),
        **written,
    }


def _run(env: dict[str, str]) -> str:
    """Run the script; return the comment body it posted (empty if none)."""
    result = subprocess.run(
        [BASH, str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    posted = Path(env["COMMENT_OUT"])
    return posted.read_text() if posted.exists() else ""


def test_a_plain_non_zero_exit_is_enriched_from_the_log(env: dict[str, str]) -> None:
    """A job that fails by exiting non-zero produces exactly one annotation —
    `Process completed with exit code 1.` — which the script drops as useless.
    Nothing else reads the log, so the run is written up as unenrichable and
    the marker stops any later night retrying it, while the failing assertion
    sits in the log the whole time."""
    body = _run(env)

    assert "test_holding_a_key_repeats" in body
    assert "1 failed, 2021 passed" in body
    assert "No failure details could be extracted." not in body
    assert f"<!-- enriched-run:{RUN_ID} -->" in body


def test_the_log_prefix_is_stripped(env: dict[str, str]) -> None:
    """`--log-failed` prefixes each line with job, step and timestamp; left in,
    they crowd the failure out of a reader's line width."""
    body = _run(env)

    assert "FAILED tests/test_render_navigation.py" in body
    assert "2026-09-06T08:00:00.1234567Z" not in body


def test_annotations_win_and_the_log_is_not_fetched(env: dict[str, str]) -> None:
    """Annotations are precise and one cheap call; the log is the fallback."""
    Path(env["ANNOTATIONS_JSON"]).write_text(
        json.dumps([{"annotation_level": "failure", "message": "the agent exited 2"}])
    )

    body = _run(env)

    assert "the agent exited 2" in body
    assert "log tail" not in body
    assert "run view" not in Path(env["GH_CALLS"]).read_text()


def test_a_run_with_no_recoverable_detail_still_records_the_marker(
    env: dict[str, str],
) -> None:
    """A session killed at the job cap fails no step, so neither source has
    anything — that run is genuinely unenrichable and must not be retried
    nightly forever."""
    Path(env["LOG_TXT"]).write_text("")

    body = _run(env)

    assert "No failure details could be extracted." in body
    assert f"<!-- enriched-run:{RUN_ID} -->" in body


def test_an_unavailable_log_does_not_abort_the_batch(env: dict[str, str]) -> None:
    """Logs expire, and `gh run view` fails outright on a deleted run. Under
    `set -e` an unguarded call would take the whole nightly step down."""
    env["LOG_FAILS"] = "1"

    body = _run(env)

    assert "No failure details could be extracted." in body
