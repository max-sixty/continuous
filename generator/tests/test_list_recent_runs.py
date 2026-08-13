"""Tests for plugins/tend-ci-runner/scripts/list-recent-runs.sh.

The window logic is the behaviour under test: the completion window resumes
at the previous successful run's start, clamps at 6h with a stderr WARNING,
and falls back to a plain 1h window outside Actions. The fake `gh` runs the
script's own `--jq` expressions against fixtures with real jq — the anchor
query's self-exclusion filter is load-bearing, so a pre-filtered fake would
assert nothing. `date` is faked with a fixed clock (macOS ships BSD date,
which lacks `-d`; the fixed clock also keeps the window edges deterministic).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts" / "list-recent-runs.sh"

# The fixed clock: 2023-11-14T22:13:20Z.
NOW = 1700000000


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


FAKE_GH = (
    GH_PREAMBLE
    + r"""case "$1 $2" in
  "workflow list")
    if [ -n "${FAIL_WORKFLOW_LIST:-}" ]; then exit 1; fi
    emit "$(cat "$WF_JSON")"
    ;;
  "run list")
    # The anchor query is the only `gh run list` carrying --status success.
    case "$*" in
      *"--status success"*)
        if [ -n "${FAIL_ANCHOR:-}" ]; then exit 1; fi
        emit "$(cat "$ANCHOR_JSON")"
        ;;
      *)
        if [ -n "${FAIL_RUNS:-}" ]; then exit 1; fi
        emit "$(cat "$RUNS_JSON")"
        ;;
    esac
    ;;
  *)
    exit 1
    ;;
esac
"""
)

# GNU-date stand-in with a fixed clock. The three forms the script uses:
#   date -u +%s                  -> $FAKE_NOW
#   date -u -d "@<epoch>" +<fmt> -> the epoch itself (+%s) or "iso(<epoch>)"
#   date -u -d "<iso>" +%s       -> looked up in $DATE_TABLE (iso=epoch lines)
FAKE_DATE = r"""#!/usr/bin/env bash
arg_d=""
fmt=""
prev=""
for a in "$@"; do
  case "$a" in
    +*) fmt="$a" ;;
  esac
  [ "$prev" = "-d" ] && arg_d="$a"
  prev="$a"
done
if [ -z "$arg_d" ]; then
  echo "$FAKE_NOW"
elif [ "${arg_d#@}" != "$arg_d" ]; then
  epoch="${arg_d#@}"
  if [ "$fmt" = "+%s" ]; then echo "$epoch"; else echo "iso($epoch)"; fi
else
  grep -F "$arg_d=" "$DATE_TABLE" | head -1 | cut -d= -f2
fi
"""


def _run_entry(run_id: int, *, updated: int, conclusion: str = "success") -> dict:
    return {
        "databaseId": run_id,
        "conclusion": conclusion,
        "createdAt": _iso(updated - 300),
        "updatedAt": _iso(updated),
    }


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Fake gh/date on PATH plus the Actions env the script reads."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH, date=FAKE_DATE)

    (tmp_path / "wf.json").write_text(json.dumps([{"name": "tend-review"}]))
    (tmp_path / "anchor.json").write_text("[]")
    (tmp_path / "runs.json").write_text("[]")
    (tmp_path / "date-table").write_text("")

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "WF_JSON": str(tmp_path / "wf.json"),
        "ANCHOR_JSON": str(tmp_path / "anchor.json"),
        "RUNS_JSON": str(tmp_path / "runs.json"),
        "DATE_TABLE": str(tmp_path / "date-table"),
        "FAKE_NOW": str(NOW),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_WORKFLOW": "review-reviewers",
        "GITHUB_RUN_ID": "999",
    }


def _anchor(env: dict[str, str], *entries: tuple[int, int]) -> None:
    """Anchor candidates as (databaseId, createdAt-epoch); table their ISOs."""
    Path(env["ANCHOR_JSON"]).write_text(
        json.dumps(
            [{"databaseId": i, "createdAt": _iso(start)} for i, start in entries]
        )
    )
    Path(env["DATE_TABLE"]).write_text(
        "".join(f"{_iso(start)}={start}\n" for _, start in entries)
    )


def _runs(env: dict[str, str], *entries: dict) -> None:
    Path(env["RUNS_JSON"]).write_text(json.dumps(list(entries)))


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True
    )


def _ids(result: subprocess.CompletedProcess[str]) -> list[int]:
    return [r["databaseId"] for r in json.loads(result.stdout)]


def test_window_floors_at_the_anchors_start(env: dict[str, str]) -> None:
    """The floor is the previous successful run's start: completions after it
    are in, completions before it and still-running runs are out."""
    _anchor(env, (555, NOW - 5400))
    _runs(
        env,
        _run_entry(1, updated=NOW - 3600),
        _run_entry(2, updated=NOW - 7200),
        _run_entry(3, updated=NOW - 600, conclusion=""),
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _ids(result) == [1]
    assert "WARNING" not in result.stderr
    anchor_calls = [
        line
        for line in Path(env["GH_CALLS"]).read_text().splitlines()
        if "--status success" in line
    ]
    assert anchor_calls and all("--repo owner/repo" in c for c in anchor_calls), (
        "the anchor query must name the workflow's own repo explicitly, not "
        "lean on cwd remote detection"
    )


def test_anchor_query_excludes_the_current_run(env: dict[str, str]) -> None:
    """A re-run attempt of the current run can already read as a completed
    success; anchoring on it would collapse the window to zero. The newest
    non-self success anchors instead."""
    _anchor(env, (999, NOW - 60), (555, NOW - 5400))
    _runs(env, _run_entry(1, updated=NOW - 3600))

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _ids(result) == [1], (
        "the window was anchored on the current run itself, collapsing it"
    )


def test_no_anchor_floors_at_6h_and_warns(env: dict[str, str]) -> None:
    """With no successful run at all, the window reaches back 6h and the
    stderr WARNING tells the caller to record a coverage gap."""
    _runs(
        env,
        _run_entry(1, updated=NOW - 18000),
        _run_entry(2, updated=NOW - 25200),
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _ids(result) == [1]
    assert "WARNING: no successful" in result.stderr


def test_stale_anchor_clamps_to_6h_and_warns(env: dict[str, str]) -> None:
    """An anchor older than 6h (a sustained outage) clamps the floor rather
    than growing the window unboundedly, and warns of the coverage gap."""
    _anchor(env, (555, NOW - 28800))
    _runs(
        env,
        _run_entry(1, updated=NOW - 18000),
        _run_entry(2, updated=NOW - 23400),
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _ids(result) == [1]
    assert "more than 6h back" in result.stderr


def test_outside_actions_uses_a_1h_window(env: dict[str, str]) -> None:
    """An ad-hoc invocation (no GITHUB_WORKFLOW) covers the past hour and
    never queries for an anchor."""
    del env["GITHUB_WORKFLOW"]
    _runs(
        env,
        _run_entry(1, updated=NOW - 1800),
        _run_entry(2, updated=NOW - 5400),
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _ids(result) == [1]
    calls = Path(env["GH_CALLS"]).read_text()
    assert "--status success" not in calls, "ad-hoc invocation queried an anchor"


def test_fetch_limit_cap_warns(env: dict[str, str]) -> None:
    """Exactly the fetch limit means the list may be truncated at its old end;
    the rows are still returned, with a WARNING against reading an all-clear."""
    _anchor(env, (555, NOW - 5400))
    _runs(env, *(_run_entry(i, updated=NOW - 600) for i in range(200)))

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert len(_ids(result)) == 200
    assert "the fetch limit" in result.stderr


@pytest.mark.parametrize("failure", ["FAIL_WORKFLOW_LIST", "FAIL_ANCHOR", "FAIL_RUNS"])
def test_api_failure_fails_loudly(env: dict[str, str], failure: str) -> None:
    """A transient API failure must fail the script, not report an empty list
    that reads as an all-clear. A later tick retries the window."""
    _anchor(env, (555, NOW - 5400))
    _runs(env, _run_entry(1, updated=NOW - 3600))
    env[failure] = "1"

    result = _run(env)

    assert result.returncode != 0, (
        f"{failure}: an API failure produced exit 0 with: {result.stdout!r}"
    )


def test_workflows_filtered_by_prefix(env: dict[str, str]) -> None:
    """Only workflows matching the prefixes are fetched (default: tend-)."""
    Path(env["WF_JSON"]).write_text(
        json.dumps([{"name": "tend-review"}, {"name": "tend-mention"}, {"name": "ci"}])
    )
    _anchor(env, (555, NOW - 5400))

    result = _run(env)

    assert result.returncode == 0, result.stderr
    fetches = [
        line
        for line in Path(env["GH_CALLS"]).read_text().splitlines()
        if line.startswith("run list") and "--status success" not in line
    ]
    assert len(fetches) == 2
    assert not any(" ci " in f for f in fetches)


def test_overlapping_prefixes_do_not_double_count(env: dict[str, str]) -> None:
    """A workflow matched by two prefixes is fetched twice; its runs must
    still appear once, or the caller reports a doubled census."""
    _anchor(env, (555, NOW - 5400))
    _runs(env, _run_entry(1, updated=NOW - 3600))

    result = _run(env, "tend-", "tend-rev")

    assert result.returncode == 0, result.stderr
    assert _ids(result) == [1], "one workflow's runs were counted once per prefix"
