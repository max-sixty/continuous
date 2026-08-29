"""Tests for plugins/tend-ci-runner/scripts/token-report.sh.

The report exists to answer "what did the spend go to?", so what is pinned
here is the grouping and the ranking: subjects come off the record each run
uploaded, and cost — not the token count — leads and orders every table.
Cache reads are ~97% of the tokens and ~60% of the bill, so a table ranked by
summed tokens ranks the cheapest work first; the fixtures below make the two
orders disagree so a regression to token-ranking fails.

The fake `gh` serves a run list and materialises each run's artifact into the
`--dir` the script passes, which is the whole of what the script needs from
GitHub. `date` is faked so the window is a fixed string.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts" / "token-report.sh"

FAKE_GH = (
    GH_PREAMBLE
    + r"""case "$1 $2" in
  "workflow list")
    emit "$(cat "$WF_JSON")"
    ;;
  "run list")
    wf=""
    prev=""
    for a in "$@"; do
      [ "$prev" = "--workflow" ] && wf="$a"
      prev="$a"
    done
    emit "$(jq -c --arg wf "$wf" '[.[] | select(.name == $wf)]' "$RUNS_JSON")"
    ;;
  "run download")
    # The artifact the run uploaded, or a non-zero exit for a run that has
    # none — the script skips those.
    dir=""
    prev=""
    for a in "$@"; do
      [ "$prev" = "--dir" ] && dir="$a"
      prev="$a"
    done
    [ -f "$USAGE_DIR/$3.json" ] || exit 1
    mkdir -p "$dir/claude-session-logs-1"
    cp "$USAGE_DIR/$3.json" "$dir/claude-session-logs-1/token-usage.json"
    ;;
  *)
    exit 1
    ;;
esac
"""
)

# The window is a fixed string; nothing here depends on the real clock.
FAKE_DATE = "#!/usr/bin/env bash\necho 2026-08-22T00:00:00Z\n"


def _record(**over: Any) -> dict[str, Any]:
    """One job's token-usage.json, as the "Token usage" step writes it."""
    return {
        "repo": "owner/repo",
        "workflow": "tend-review",
        "run_id": 1,
        "run_attempt": 1,
        "event": "pull_request_target",
        "number": 851,
        "head_sha": "head0000",
        "input_tokens": 10,
        "output_tokens": 100,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 10000,
        "turns": 3,
        "model": "opus",
        "cost_usd": 1.0,
        "partial": False,
        **over,
    }


class Report:
    """A configured run of the script: add runs, then read the two outputs."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        self._runs: list[dict[str, Any]] = []
        self._usage_dir = tmp_path / "usage"
        self._usage_dir.mkdir()
        self._env = {
            "PATH": tool_path(fake_bin(tmp_path, gh=FAKE_GH, date=FAKE_DATE)),
            "GH_CALLS": str(tmp_path / "gh-calls.log"),
            "WF_JSON": str(tmp_path / "wf.json"),
            "RUNS_JSON": str(tmp_path / "runs.json"),
            "USAGE_DIR": str(self._usage_dir),
        }

    def add(
        self, run_id: int, *, workflow: str = "tend-review", **over: Any
    ) -> "Report":
        """A completed run and the artifact it uploaded."""
        self._runs.append(
            {
                "databaseId": run_id,
                "conclusion": "success",
                "createdAt": f"2026-08-2{run_id % 10}T12:00:00Z",
                "name": workflow,
            }
        )
        (self._usage_dir / f"{run_id}.json").write_text(
            json.dumps(_record(run_id=run_id, workflow=workflow, **over))
        )
        return self

    def add_run_without_artifact(self, run_id: int) -> "Report":
        self._runs.append(
            {
                "databaseId": run_id,
                "conclusion": "cancelled",
                "createdAt": "2026-08-25T12:00:00Z",
                "name": "tend-review",
            }
        )
        return self

    def run(self) -> tuple[dict[str, Any], list[list[str]]]:
        """The JSON on stdout, and the stderr summary split into cells."""
        workflows = sorted({run["name"] for run in self._runs})
        Path(self._env["WF_JSON"]).write_text(
            json.dumps([{"name": name} for name in workflows])
        )
        Path(self._env["RUNS_JSON"]).write_text(json.dumps(self._runs))
        result = subprocess.run(
            [BASH, str(SCRIPT), "168"],
            env=self._env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        rows = [line.split() for line in result.stderr.splitlines() if line.strip()]
        return json.loads(result.stdout), rows


@pytest.fixture
def report(tmp_path: Path) -> Report:
    return Report(tmp_path)


def _table(rows: list[list[str]], header: str) -> list[list[str]]:
    """The rows under the table whose first heading is *header*."""
    start = next(i for i, row in enumerate(rows) if row[0] == header) + 1
    out = []
    for row in rows[start:]:
        if row[0] in {"WORKFLOW", "SUBJECT", "RUN"}:
            break
        out.append(row)
    return out


def test_the_record_carries_the_subject_so_no_join_is_needed(report: Report) -> None:
    """Each run's PR, event and commit ride on its own artifact.

    Without them every question about what the spend went to costs a second
    query per run; the run list the script already makes cannot answer them —
    its `headSha` for a `pull_request_target` run is the base branch, not the
    commit the workflow checked out.
    """
    report.add(1).add(2, number=852, head_sha="head0002")
    output, _ = report.run()

    runs = {run["run_id"]: run for run in output["runs"]}
    assert runs[1]["number"] == 851
    assert runs[1]["subject"] == "#851"
    assert runs[1]["event"] == "pull_request_target"
    assert runs[1]["repo"] == "owner/repo"
    assert runs[1]["head_sha"] == "head0000"
    assert runs[2]["subject"] == "#852"


def test_repeat_runs_on_one_subject_collapse_into_one_row(report: Report) -> None:
    """The costly pattern is many runs on one PR, which is one row with a count.

    Three reviews of #851 and one of #852: the subject table has to show two
    rows, the repeat one carrying its run count and its summed cost.
    """
    for run_id in (1, 2, 3):
        report.add(run_id, cost_usd=2.0)
    report.add(4, number=852, cost_usd=1.0)
    _, rows = report.run()

    assert _table(rows, "SUBJECT") == [
        ["#851", "3", "$6.00", "tend-review", "30K"],
        ["#852", "1", "$1.00", "tend-review", "10K"],
    ]


def test_tables_are_ranked_by_cost_not_by_token_count(report: Report) -> None:
    """Cache reads dominate the count and not the bill, so ranking by tokens
    ranks the cheapest work first.

    `tend-nightly` here reads ten times the cache for a tenth of the cost —
    the shape of a real fleet's spend, and the two orders are opposite.
    """
    report.add(1, workflow="tend-nightly", cost_usd=0.5, cache_read_input_tokens=999999)
    report.add(2, workflow="tend-review", cost_usd=9.0, cache_read_input_tokens=1000)
    _, rows = report.run()

    workflows = [row[0] for row in _table(rows, "WORKFLOW")]
    assert workflows == ["tend-review", "tend-nightly"], (
        "the costliest workflow leads; ranking by cache reads inverts this"
    )
    assert _table(rows, "WORKFLOW")[0][2] == "$9.00", "cost is the second column"


def test_a_cost_unknown_run_reads_as_a_floor_not_as_free(report: Report) -> None:
    """A cancelled run's tokens are real and its cost is unrecoverable.

    Cost now leads every table, so a reconstructed run must never read as
    cheap: its cells carry `+`, and the total names how many runs it covers.
    """
    report.add(1, cost_usd=None, partial=True)
    report.add(2, number=852, cost_usd=4.0)
    output, rows = report.run()

    assert output["totals"]["partial_runs"] == 1
    total = next(row for row in rows if row[0] == "Total")
    assert total[2] == "$4.00+", "the headline cost is a floor while a run is unpriced"
    assert " ".join(total).endswith("(1 of 2 runs cost-unknown)")
    assert _table(rows, "SUBJECT")[1] == ["#851", "1", "$0.00+", "tend-review", "10K"]


def test_a_run_whose_record_predates_the_subject_fields(report: Report) -> None:
    """An artifact still in the window from before this change still reports.

    Its counts are all it has, so it groups under `?` rather than dropping out
    of the totals.
    """
    report.add(1, number=None, head_sha=None, repo=None, event=None)
    output, rows = report.run()

    assert output["runs"][0]["subject"] == "?"
    assert output["totals"]["cost_usd"] == 1.0
    assert _table(rows, "SUBJECT") == [["?", "1", "$1.00", "tend-review", "10K"]]


def test_a_run_with_no_artifact_is_skipped(report: Report) -> None:
    """A run that uploaded nothing — a codex-harness run, or one killed before
    the token step — contributes no row rather than a zero one."""
    report.add(1).add_run_without_artifact(2)
    output, _ = report.run()

    assert [run["run_id"] for run in output["runs"]] == [1]


def test_the_subject_table_stops_at_the_top_and_says_so(report: Report) -> None:
    """Past the top the tail is one-run subjects; the JSON on stdout has them.

    The cap and the note it prints are the same value, so a reader is never
    told a count the table contradicts.
    """
    for run_id in range(1, 26):
        report.add(run_id, number=1000 + run_id, cost_usd=float(run_id))
    output, rows = report.run()

    subjects = _table(rows, "SUBJECT")
    assert len(subjects) == 20
    assert subjects[0][0] == "#1025", "the costliest subject leads"
    assert len(output["runs"]) == 25
    assert any("costliest of 25" in " ".join(row) for row in rows)
