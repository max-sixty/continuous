"""Tests for the composite actions' shared step bodies (shared/steps/*.sh).

These scripts run as `bash <script>` inside both harness actions, so a
non-zero exit fails the step and turns an otherwise-successful agent run red.
They aren't part of the generator package; the tests live here because this is
the repo's only Python suite, and shellcheck (pre-commit) can't catch runtime
behaviour.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MARK_NOTIFICATION_READ = REPO_ROOT / "shared" / "steps" / "mark-notification-read.sh"

# `gh api` stand-in. Records every invocation so a test can assert which calls
# the script made, and fails the run-metadata fetch when FAIL_RUN_META is set.
FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$2" in
  repos/*/actions/runs/*)
    [ -n "${FAIL_RUN_META:-}" ] && exit 1
    echo "$FAKE_RUN_STARTED_AT"
    ;;
  notifications)
    cat "$NOTIFICATIONS_JSON"
    ;;
  notifications/threads/*)
    ;;
  *)
    exit 1
    ;;
esac
"""


@pytest.fixture
def gh_env(tmp_path: Path) -> dict[str, str]:
    """A fake `gh` on PATH plus the Actions env the script reads."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"issue": {"number": 7}}))

    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "12345",
        # Deliberately not `RUN_STARTED_AT`: the script assigns that name, and
        # an inherited value would let the happy-path tests pass even if the
        # fetched timestamp were never used.
        "FAKE_RUN_STARTED_AT": "2026-01-02T00:00:00Z",
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(MARK_NOTIFICATION_READ)],
        env=env,
        capture_output=True,
        text=True,
    )


def _calls(env: dict[str, str]) -> list[str]:
    return Path(env["GH_CALLS"]).read_text().splitlines()


def _notifications(tmp_path: Path, updated_at: str) -> str:
    """One unread notification for issue 7 of owner/repo."""
    path = tmp_path / "notifications.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "999",
                    "updated_at": updated_at,
                    "subject": {
                        "url": "https://api.github.com/repos/owner/repo/issues/7"
                    },
                }
            ]
        )
    )
    return str(path)


def test_mark_notification_read_tolerates_run_metadata_failure(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """A transient failure fetching `run_started_at` must not fail the step.

    The script runs under `set -e`, so an unguarded `gh api` there aborts it
    non-zero. The step is gated on `if: success()` in both harness actions, so
    that exit turns a fully-successful agent run into a red job. The correct
    disposition is to skip this cycle and leave the thread unread — the
    scheduled tend-notifications poll picks it up.
    """
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-01-01T00:00:00Z")
    gh_env["FAIL_RUN_META"] = "1"

    result = _run(gh_env)

    assert result.returncode == 0, (
        f"script aborted on a transient run-metadata error (exit "
        f"{result.returncode}); stderr:\n{result.stderr}"
    )
    # Without the timestamp the `updated_at <= started` guard can't be
    # evaluated, so nothing may be marked read.
    assert not any("-X PATCH" in c for c in _calls(gh_env)), (
        "marked a thread read without knowing when the run started"
    )


def test_mark_notification_read_marks_thread_predating_the_run(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """The happy path still marks a thread whose activity predates the run."""
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-01-01T00:00:00Z")

    result = _run(gh_env)

    assert result.returncode == 0, result.stderr
    assert "api notifications/threads/999 -X PATCH" in _calls(gh_env)


def test_mark_notification_read_leaves_activity_newer_than_the_run(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """Activity that arrived after the run started stays unread."""
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-03-01T00:00:00Z")

    result = _run(gh_env)

    assert result.returncode == 0, result.stderr
    assert not any("-X PATCH" in c for c in _calls(gh_env))


# --- rate-limit-preflight.sh -------------------------------------------------

RATE_LIMIT_PREFLIGHT = REPO_ROOT / "shared" / "steps" / "rate-limit-preflight.sh"

# `gh api` stand-in returning the four counts the preflight reads. The two
# search/issues calls are told apart by the `..` date range only the baseline
# query carries. FAKE_ prefixes keep these clear of the names the script
# assigns, so a test can't pass on an inherited value it never fetched.
FAKE_GH_COUNTS = """#!/usr/bin/env bash
case "$2" in
  repos/*/pulls*) echo "$FAKE_BURST_PRS" ;;
  repos/*/issues*) echo "$FAKE_BURST_ISSUES" ;;
  *created:*..*) echo "$FAKE_BASELINE" ;;
  search/issues*) echo "$FAKE_TODAY" ;;
  *) exit 1 ;;
esac
"""

# A 6-day baseline of 17 is what the bot actually carried into 2026-08-05: it
# puts the spike tier at 10 + 17/3 = 15 and the hard tier at 10 + 17 = 27.
BASELINE = 17


def _preflight(
    tmp_path: Path,
    *,
    today: int,
    baseline: int = BASELINE,
    burst_prs: int = 0,
    burst_issues: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the preflight against fixed counts; return the result and GITHUB_ENV."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH_COUNTS)
    gh.chmod(0o755)
    github_env = tmp_path / "github-env"
    github_env.write_text("")

    result = subprocess.run(
        ["bash", str(RATE_LIMIT_PREFLIGHT)],
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_ENV": str(github_env),
            "BOT_NAME": "bot",
            "FAKE_TODAY": str(today),
            "FAKE_BASELINE": str(baseline),
            "FAKE_BURST_PRS": str(burst_prs),
            "FAKE_BURST_ISSUES": str(burst_issues),
        },
        capture_output=True,
        text=True,
    )
    return result, github_env.read_text()


def test_rate_limit_spike_tier_pauses_creation_without_aborting(
    tmp_path: Path,
) -> None:
    """The spike tier lets the run proceed and pauses creation instead.

    Aborting here took every workflow on the repo down for the rest of the UTC
    day — `tend-review` and `tend-mention` included, which answer humans and
    create nothing, so they cannot contribute to the count being enforced. This
    is the 2026-08-05 case exactly: 16 items against a spike limit of 15.
    """
    result, github_env = _preflight(tmp_path, today=16)

    assert result.returncode == 0, (
        f"spike tier aborted the run (exit {result.returncode}); "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "TEND_CREATION_PAUSED_NOTE" in github_env, (
        f"no creation pause exported; GITHUB_ENV:\n{github_env}"
    )
    assert "do not open a new issue or pull request" in github_env


def test_rate_limit_hard_tier_still_aborts(tmp_path: Path) -> None:
    """A slow runaway crosses the hard tier and is stopped deterministically.

    The spike tier is advisory once it reaches the model, so this is the gate a
    prompt-injected session cannot talk its way past.
    """
    result, github_env = _preflight(tmp_path, today=28)

    assert result.returncode != 0, (
        f"runaway volume did not abort the run; stdout:\n{result.stdout}"
    )
    assert "hard limit of 27" in result.stdout
    assert "TEND_CREATION_PAUSED_NOTE" not in github_env, (
        "exported a pause note on a run that aborts anyway"
    )


def test_rate_limit_under_spike_tier_is_a_clean_pass(tmp_path: Path) -> None:
    """Normal volume neither aborts nor pauses creation."""
    result, github_env = _preflight(tmp_path, today=3)

    assert result.returncode == 0, result.stderr
    assert "Rate limit check passed" in result.stdout
    assert "TEND_CREATION_PAUSED_NOTE" not in github_env


def test_rate_limit_burst_aborts_regardless_of_daily_volume(tmp_path: Path) -> None:
    """The 20-minute burst check is untouched: it still aborts the run."""
    result, github_env = _preflight(tmp_path, today=3, burst_prs=11)

    assert result.returncode != 0, (
        f"burst of 11 PRs in 20 minutes did not abort; stdout:\n{result.stdout}"
    )
    assert "TEND_CREATION_PAUSED_NOTE" not in github_env
