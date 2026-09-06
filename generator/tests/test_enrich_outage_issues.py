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

# The log API returns a colour escape as the literal characters `^[[42m`, not an
# ESC byte, so nothing downstream renders it away.
COLOURED_TAIL = _log(
    "shellcheck.................................................^[[42mPassed^[[m",
    "^[[31mFAILED^[[0m tests/test_sandbox.py::test_proxy_env",
    "##[error]Process completed with exit code 1.",
)

# The first line of each step's log carries a byte-order mark ahead of the
# timestamp. It only reaches the window when the failing step is shorter than
# the window itself.
BOM_TAIL = (
    "test\tRun the action\t\ufeff2026-09-06T08:00:00.1234567Z curl: (35) Recv failure\n"
    + _log("##[error]Process completed with exit code 35.")
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


def test_terminal_colour_escapes_are_stripped(env: dict[str, str]) -> None:
    """Any tool that forces colour under CI (pre-commit, cargo, npm) writes
    them, and unrendered they bury the diagnosis the fallback exists to
    surface."""
    Path(env["LOG_TXT"]).write_text(COLOURED_TAIL)

    body = _run(env)

    assert "^[" not in body
    assert "shellcheck.................................................Passed" in body
    assert "FAILED tests/test_sandbox.py::test_proxy_env" in body


def test_the_step_boundary_bom_does_not_defeat_the_timestamp_strip(
    env: dict[str, str],
) -> None:
    """The mark sits ahead of the timestamp, so an anchored strip misses both."""
    Path(env["LOG_TXT"]).write_text(BOM_TAIL)

    body = _run(env)

    assert "curl: (35) Recv failure" in body
    assert "2026-09-06T08:00:00.1234567Z" not in body
    assert "\ufeff" not in body


def _fence_lines(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("```")]


def _many_runs(env: dict[str, str], count: int) -> list[str]:
    """Point the tracker at `count` unenriched runs."""
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


def test_an_oversized_batch_is_truncated_rather_than_rejected(
    env: dict[str, str],
) -> None:
    """A body over GitHub's 65536-char limit is refused with a 422, and under
    `set -e` that takes the whole pass down. No marker is written for any run in
    the batch, so the next night rebuilds the identical batch and fails the same
    way — the enrichment jams permanently."""
    runs = _many_runs(env, 6)
    Path(env["LOG_TXT"]).write_text(
        _log(*(["x" * 600] * 30), "##[error]Process completed with exit code 1.")
    )

    body = _run(env)

    assert len(body) < 65536
    assert "_Truncated" in body
    assert f"<!-- enriched-run:{runs[0]} -->" in body
    assert f"<!-- enriched-run:{runs[-1]} -->" not in body


def test_truncation_keeps_whole_sections(env: dict[str, str]) -> None:
    """Cutting the batch at a byte offset lands mid-section: the body ends on an
    unclosed fence, the note renders inside the code block, and the run whose
    section was cut loses its marker. Cutting between runs keeps each posted
    section — fences, marker and all — intact."""
    runs = _many_runs(env, 6)
    Path(env["LOG_TXT"]).write_text(
        _log(*(["x" * 600] * 30), "##[error]Process completed with exit code 1.")
    )

    body = _run(env)

    assert len(_fence_lines(body)) % 2 == 0
    for run in runs:
        section = f"### [Run {run}]"
        if section in body:
            assert f"<!-- enriched-run:{run} -->" in body
    assert body.rstrip().endswith(
        "_Truncated; the remaining runs are enriched by a later batch._"
    )


def test_one_huge_annotation_cannot_fill_the_body(env: dict[str, str]) -> None:
    """A linter annotates per finding, so a single job's message is unbounded.
    Left that way it fills the batch on its own — and a batch cut inside that
    one section posts a body with no marker at all, so the next night rebuilds
    it and posts the identical comment, every night."""
    Path(env["ANNOTATIONS_JSON"]).write_text(
        json.dumps(
            [
                {
                    "annotation_level": "failure",
                    "message": "E501 line too long " * 4000,
                },
                {"annotation_level": "failure", "message": "\n".join(["F401"] * 4000)},
            ]
        )
    )

    body = _run(env)

    assert len(body) < 65536
    assert f"<!-- enriched-run:{RUN_ID} -->" in body
    assert "E501 line too long" in body
    assert len(_fence_lines(body)) % 2 == 0


def test_a_matrix_of_failed_jobs_cannot_fill_the_body(env: dict[str, str]) -> None:
    """One run's section is the unit the batch cap keeps whole, and a matrix
    contributes one job section per failed leg."""
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
            [{"annotation_level": "failure", "message": "\n".join(["y" * 600] * 30)}]
        )
    )

    body = _run(env)

    assert len(body) < 65536
    assert f"<!-- enriched-run:{RUN_ID} -->" in body
    assert "_Remaining failed jobs omitted._" in body
    assert len(_fence_lines(body)) % 2 == 0


def test_an_issue_with_nothing_new_posts_nothing_and_exits_clean(
    env: dict[str, str],
) -> None:
    """Every run already enriched leaves an empty batch. As the last command of
    the loop body, a bare `[ -s ... ] && gh issue comment` returns non-zero
    there and ends the whole pass under `set -e`."""
    Path(env["ISSUE_JSON"]).write_text(
        json.dumps(
            {
                "body": f"https://github.com/owner/repo/actions/runs/{RUN_ID}",
                "comments": [{"body": f"<!-- enriched-run:{RUN_ID} -->"}],
            }
        )
    )

    assert _run(env) == ""
