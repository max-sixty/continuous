"""Tests for `plugins/tend-ci-runner/scripts/list-recent-runs.sh`.

The script decides which CI runs a `review-reviewers`/`review-runs` session
analyzes, so a wrong window is invisible in the output — it reads as a normal
run list. It isn't part of the generator package; the test lives here because
this is the repo's only Python suite, and shellcheck can't catch window
arithmetic.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIST_RECENT_RUNS = (
    REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts" / "list-recent-runs.sh"
)

# `gh` stand-in covering the three calls the script makes: the workflow
# discovery list, the previous-run window anchor (the only `run list` carrying
# `--status`), and the per-workflow run list.
FAKE_GH = """#!/usr/bin/env bash
case "$1 $2" in
  "workflow list") echo '[{"name":"tend-review"}]' ;;
  "run list")
    if [[ "$*" == *"--status"* ]]; then
      echo "$FAKE_PREV_RUN_STARTED_AT"
    else
      cat "$FAKE_RUNS_JSON"
    fi
    ;;
  *) echo "unexpected gh invocation: $*" >&2; exit 1 ;;
esac
"""


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _intended_tick(now: datetime, cron_minute: int) -> datetime:
    """The tick the script anchors on, by the same rule the script uses."""
    this_hour_tick = now.replace(minute=cron_minute, second=0, microsecond=0)
    if now < this_hour_tick:
        return this_hour_tick - timedelta(hours=1)
    return this_hour_tick


@pytest.fixture
def harness(tmp_path: Path):
    """Fake `gh` on PATH plus a builder for the Actions env the script reads."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)

    def run(schedule: str | None, runs: list[dict], prev_started_at: str = "") -> list:
        runs_json = tmp_path / "runs.json"
        runs_json.write_text(json.dumps(runs))

        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "FAKE_RUNS_JSON": str(runs_json),
            "FAKE_PREV_RUN_STARTED_AT": prev_started_at,
            "GITHUB_WORKFLOW": "review-reviewers",
            "GITHUB_RUN_ID": "999",
        }
        if schedule is not None:
            event = tmp_path / "event.json"
            event.write_text(json.dumps({"schedule": schedule}))
            env["GITHUB_EVENT_PATH"] = str(event)

        result = subprocess.run(
            ["bash", str(LIST_RECENT_RUNS), "tend-"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return sorted(entry["databaseId"] for entry in json.loads(result.stdout))

    return run


def _run_record(run_id: int, finished: datetime, conclusion: str | None = "success"):
    return {
        "databaseId": run_id,
        "conclusion": conclusion,
        "createdAt": _iso(finished - timedelta(minutes=5)),
        "updatedAt": _iso(finished),
    }


def test_window_is_half_open_at_the_cron_tick(harness):
    """A run that finished after the tick belongs to the next cycle, not this one.

    The floor advances by exactly one period per cycle, so a window with no
    ceiling is as much wider than a period as the scheduler is late, and the
    next cycle lists that tail again — a second agent survey of runs already
    analyzed.
    """
    now = datetime.now(UTC)
    # Put the tick ~30 min from now in either direction, so the test can't race
    # a tick rollover between computing fixtures and the script reading its own
    # clock.
    cron_minute = (now.minute + 30) % 60
    tick = _intended_tick(now, cron_minute)

    listed = harness(
        f"{cron_minute} * * * *",
        [
            _run_record(1, tick - timedelta(hours=1, minutes=5)),  # before floor
            _run_record(2, tick - timedelta(minutes=59)),  # in window
            _run_record(3, tick - timedelta(minutes=2)),  # in window
            _run_record(4, tick + timedelta(minutes=2)),  # after tick
            _run_record(5, tick - timedelta(minutes=1), conclusion=None),  # running
        ],
        prev_started_at=_iso(tick - timedelta(minutes=59, seconds=30)),
    )

    assert listed == [2, 3]


def test_next_cycle_picks_up_what_the_ceiling_deferred(harness):
    """The deferred run is the next cycle's first item — deferred, not dropped."""
    now = datetime.now(UTC)
    cron_minute = (now.minute + 30) % 60
    tick = _intended_tick(now, cron_minute)
    # A run that finished just after the *previous* tick: the ceiling kept it
    # out of that cycle, and this cycle's floor is exactly that tick.
    deferred = tick - timedelta(hours=1) + timedelta(minutes=2)

    listed = harness(
        f"{cron_minute} * * * *",
        [_run_record(6, deferred)],
        prev_started_at=_iso(tick - timedelta(minutes=59, seconds=30)),
    )

    assert listed == [6]


def test_non_periodic_cron_keeps_the_now_anchored_window_uncapped(harness):
    """Off the cron path there is no next cycle to hand a tail to."""
    now = datetime.now(UTC)

    listed = harness(
        "*/15 * * * *",  # no constant period — falls back to now-anchored 1h
        [
            _run_record(7, now - timedelta(minutes=10)),
            _run_record(8, now - timedelta(hours=2)),
        ],
    )

    assert listed == [7]
