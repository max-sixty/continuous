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


RATE_LIMIT_PREFLIGHT = REPO_ROOT / "shared" / "steps" / "rate-limit-preflight.sh"

# `gh` stand-in for the rate-limit preflight. Unlike FAKE_GH it runs the
# script's own `--jq` expression against a fixture with real jq, because that
# filter *is* the behaviour under test: which closes count as an approval. A
# fake that returned a pre-filtered actor list would assert nothing.
FAKE_GH_RATE_LIMIT = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_CALLS"

jq_expr=""
prev=""
for arg in "$@"; do
  [ "$prev" = "--jq" ] && jq_expr="$arg"
  prev="$arg"
done

emit() {
  if [ -n "$jq_expr" ]; then
    printf '%s' "$1" | jq -r "$jq_expr"
  else
    printf '%s' "$1"
  fi
}

case "$1" in
  api)
    case "$2" in
      *"/timeline") emit "$(cat "$TIMELINE_JSON")" ;;
      *"/pulls?"*) emit '[]' ;;
      *"/issues?creator="*) emit '[]' ;;
      "search/issues?"*)
        # The baseline query is the one carrying a `created:from..to` range.
        case "$2" in
          *".."*) emit "{\"total_count\":${FAKE_PAST_POSTS}}" ;;
          *) emit "{\"total_count\":${FAKE_TODAY_POSTS}}" ;;
        esac
        ;;
      *) exit 1 ;;
    esac
    ;;
  issue)
    case "$2" in
      list) emit "$(cat "$PAUSE_ISSUES_JSON")" ;;
      create | comment | reopen | close) ;;
      *) exit 1 ;;
    esac
    ;;
  label) ;;
  *) exit 1 ;;
esac
"""

# The script is written for the Ubuntu runners' GNU date; macOS ships BSD
# date, which has no `-d`. Fixed values also make the day-scoping assertions
# deterministic: "today" is 2026-01-02.
FAKE_DATE = r"""#!/usr/bin/env bash
case "$*" in
  *"20 minutes ago"*) echo "2026-01-02T11:40:00Z" ;;
  *"yesterday"*) echo "2026-01-01" ;;
  *"6 days ago"*) echo "2025-12-27" ;;
  *"%Y-%m-%dT%H:%M:%SZ"*) echo "2026-01-02T12:00:00Z" ;;
  *) echo "2026-01-02" ;;
esac
"""

# The preflight jitters before its check-then-act; a real sleep would add up
# to 30s per test.
FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"

TODAY = "2026-01-02"


def _closed_event(login: str, actor_type: str = "User", day: str = TODAY) -> dict:
    return {
        "event": "closed",
        "actor": {"login": login, "type": actor_type},
        "created_at": f"{day}T09:00:00Z",
    }


@pytest.fixture
def rate_limit_env(tmp_path: Path) -> dict[str, str]:
    """Fake gh/date/sleep on PATH, plus the Actions env the preflight reads."""
    import shutil

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in (
        ("gh", FAKE_GH_RATE_LIMIT),
        ("date", FAKE_DATE),
        ("sleep", FAKE_SLEEP),
    ):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)

    # Both the fake gh and the script itself shell out to jq.
    jq = shutil.which("jq")
    assert jq, "jq is required for these tests"

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 851}}))

    timeline = tmp_path / "timeline.json"
    timeline.write_text("[]")
    pause_issues = tmp_path / "pause-issues.json"
    pause_issues.write_text("[]")

    return {
        "PATH": f"{bindir}:{Path(jq).parent}:/usr/bin:/bin",
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "TIMELINE_JSON": str(timeline),
        "PAUSE_ISSUES_JSON": str(pause_issues),
        # past=15 puts the base limit at 10 + 15/3 = 15.
        "FAKE_PAST_POSTS": "15",
        "FAKE_TODAY_POSTS": "10",
        "BOT_NAME": "tend-agent",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_EVENT_PATH": str(event),
    }


def _run_preflight(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RATE_LIMIT_PREFLIGHT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _approve(env: dict[str, str], *events: dict, issue: int = 42) -> None:
    """Put a pause issue on the label, with the given timeline events."""
    Path(env["PAUSE_ISSUES_JSON"]).write_text(json.dumps([{"number": issue}]))
    Path(env["TIMELINE_JSON"]).write_text(json.dumps(list(events)))


def test_rate_limit_passes_under_the_limit(rate_limit_env: dict[str, str]) -> None:
    """Under the base limit nothing is looked up and nothing is filed."""
    result = _run_preflight(rate_limit_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(rate_limit_env)
    assert not any(c.startswith("issue ") for c in calls), (
        f"touched an issue while under the limit: {calls}"
    )


def test_rate_limit_files_an_issue_when_unapproved(
    rate_limit_env: dict[str, str],
) -> None:
    """Over the limit with no approval: refuse, and file the issue that says so."""
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert any(c.startswith("issue create") for c in _calls(rate_limit_env))


def test_rate_limit_human_close_doubles_the_ceiling(
    rate_limit_env: dict[str, str],
) -> None:
    """One close by a person takes the ceiling from 15 to 30."""
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("maintainer"))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 0, result.stderr
    assert "ceiling 30" in result.stdout


def test_rate_limit_bot_cannot_approve_itself(rate_limit_env: dict[str, str]) -> None:
    """The security property: the bot closing its own issue is not an approval.

    The bot has `issues: write` and authors this issue, so it *can* close it.
    What stops that being self-approval is this filter, not an instruction in
    a prompt — which is why it is asserted against the real jq expression.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("tend-agent"))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, (
        "the bot approved itself by closing its own pause issue"
    )


def test_rate_limit_github_app_cannot_approve(rate_limit_env: dict[str, str]) -> None:
    """A close by an App — `github-actions[bot]` — is not an approval either.

    It is not the bot account, so the login check alone would let a workflow
    holding `GITHUB_TOKEN` wave the limit through.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("github-actions[bot]", actor_type="Bot"))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "a GitHub App counted as an approving human"


def test_rate_limit_yesterdays_approval_does_not_carry(
    rate_limit_env: dict[str, str],
) -> None:
    """Approvals are scoped to today, since the count they lift resets daily."""
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("maintainer", day="2026-01-01"))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "yesterday's approval lifted today's ceiling"


def test_rate_limit_reopens_rather_than_refiling(
    rate_limit_env: dict[str, str],
) -> None:
    """Past the doubled ceiling the existing issue is reopened, not duplicated."""
    rate_limit_env["FAKE_TODAY_POSTS"] = "40"
    _approve(rate_limit_env, _closed_event("maintainer"))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert any(c.startswith("issue reopen 42") for c in calls), calls
    assert not any(c.startswith("issue create") for c in calls), (
        f"filed a second pause issue instead of reopening #42: {calls}"
    )
