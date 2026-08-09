"""Tests for the workflow step bodies shipped as shell scripts.

Two homes, one testing need: the composite actions' shared steps
(shared/steps/*.sh) run as `bash <script>` inside both harness actions, and
the generator's template scripts (generator/src/tend/templates/*.sh) are
inlined into generated workflows. In both, a non-zero exit fails the step, and
shellcheck (pre-commit) can't catch runtime behaviour; this is the repo's only
Python suite, so the tests live here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MARK_NOTIFICATION_READ = REPO_ROOT / "shared" / "steps" / "mark-notification-read.sh"


def _fake_bin(tmp_path: Path, **scripts: str) -> Path:
    """Write executable command stand-ins (gh, date, …); return the PATH dir."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in scripts.items():
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)
    return bindir


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
    bindir = _fake_bin(tmp_path, gh=FAKE_GH)

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


def _comments(env: dict[str, str]) -> str:
    """Every comment body the fake `gh` was handed on stdin, concatenated."""
    return Path(env["COMMENT_BODIES"]).read_text()


# The Run cell of a row generated under the fixtures' GITHUB_RUN_ID. What a
# carried-over row is recognised by, on either record.
RUN_LINK = "[workflow run](https://github.com/owner/repo/actions/runs/12345)"


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


def test_mark_notification_read_treats_a_null_timestamp_as_absent(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """A 200 whose body lacks `run_started_at` must be handled like a failure.

    `gh --jq` prints the literal `null` for a missing field, which is non-empty
    and so survives the `-z` guard. It then reaches the jq comparison as a
    string, and every ISO-8601 timestamp sorts before `null` by codepoint — so
    the `updated_at <= $started` filter matches every thread and the run marks
    read the mid-run activity the guard exists to preserve. The notification
    here is dated two months *after* the run, so a PATCH can only come from
    that inversion.
    """
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-03-01T00:00:00Z")
    gh_env["FAKE_RUN_STARTED_AT"] = "null"

    result = _run(gh_env)

    assert result.returncode == 0, result.stderr
    assert not any("-X PATCH" in c for c in _calls(gh_env)), (
        "marked a thread read against a `null` run_started_at"
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
      *"/events?"*) emit "$(cat "$TIMELINE_JSON")" ;;
      user)
        [ -n "${FAIL_WHOAMI:-}" ] && exit 1
        emit "{\"login\":\"tend-agent\",\"id\":${FAKE_BOT_ID}}"
        ;;
      *"/pulls?"*)
        # Built through jq so the script's own burst filter is what counts them.
        emit "$(jq -nc --argjson n "${FAKE_RECENT_PRS:-0}" \
          '[range($n) | {user: {login: "tend-agent"}, created_at: "2099-01-01T00:00:00Z"}]')"
        ;;
      *"/issues?creator="*) emit '[]' ;;
      repos/*/issues/*)
        # The reconciler's primary-key probe. Serves whatever the fixture put
        # at that number, so the script's own `--jq` decides whether it counts.
        emit "$(jq -c --argjson n "${2##*/}" \
          'map(select(.number == $n)) | .[0] // {"number":0}' "$PROBE_ISSUES_JSON")"
        ;;
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
      list)
        # Fail the list calls in [FROM, UNTIL], so the spike block's two reads
        # can be failed in any combination: FROM=1 alone fails both, FROM=2
        # spares the first, and FROM=1 UNTIL=1 spares the re-read. UNTIL is
        # unbounded by default, which keeps "the list is simply down" open
        # ended rather than pinned to an exact call count.
        if [ -n "${FAIL_ISSUE_LIST_FROM:-}" ]; then
          n=$(( $(cat "$LIST_CALLS" 2>/dev/null || echo 0) + 1 ))
          echo "$n" > "$LIST_CALLS"
          if [ "$n" -ge "$FAIL_ISSUE_LIST_FROM" ] \
            && { [ -z "${FAIL_ISSUE_LIST_UNTIL:-}" ] || [ "$n" -le "$FAIL_ISSUE_LIST_UNTIL" ]; }; then
            exit 1
          fi
        fi
        emit "$(cat "$PAUSE_ISSUES_JSON")"
        ;;
      create)
        # An `if` rather than `[ ... ] && exit 1`: with nothing after it, the
        # failed test would become the branch's status and every create would
        # report failure.
        if [ -n "${FAIL_ISSUE_CREATE:-}" ]; then exit 1; fi
        # `gh issue create` prints the new issue's URL; the reconciler reads its
        # number off the end of it.
        echo "https://github.com/owner/repo/issues/${FAKE_NEW_ISSUE}"
        ;;
      view) emit "$(cat "$KEEPER_JSON")" ;;
      comment)
        if [ -n "${FAIL_ISSUE_COMMENT:-}" ]; then exit 1; fi
        # Comment bodies arrive on stdin (`-F -`), not in the args, so they are
        # captured rather than dropped: the carry-over row is asserted on.
        cat >> "$COMMENT_BODIES"
        ;;
      reopen | close) ;;
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
BOT_ID = 4242
PAUSE_TITLE = "Bot rate limit reached"
PAUSE_LABEL = "tend-rate-limit"
# The label goes on when the preflight files the issue; approvals are closes
# after that moment.
LABELLED_AT = f"{TODAY}T08:00:00Z"


def _probe_issue(
    number: int,
    *,
    title: str,
    label: str,
    login: str = "tend-agent",
    state: str = "open",
) -> dict:
    """One issue as `GET /issues/{n}` returns it, for the reconciler's probe."""
    return {
        "number": number,
        "title": title,
        "state": state,
        "user": {"login": login},
        "labels": [{"name": label}],
    }


def _closed_event(
    login: str,
    actor_type: str = "User",
    day: str = TODAY,
    actor_id: int = 99,
) -> dict:
    return {
        "event": "closed",
        "actor": {"login": login, "id": actor_id, "type": actor_type},
        "created_at": f"{day}T09:00:00Z",
    }


@pytest.fixture
def rate_limit_env(tmp_path: Path) -> dict[str, str]:
    """Fake gh/date/sleep on PATH, plus the Actions env the preflight reads."""
    bindir = _fake_bin(
        tmp_path, gh=FAKE_GH_RATE_LIMIT, date=FAKE_DATE, sleep=FAKE_SLEEP
    )

    # Both the fake gh and the script itself shell out to jq.
    jq = shutil.which("jq")
    assert jq, "jq is required for these tests"

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 851}}))

    timeline = tmp_path / "timeline.json"
    timeline.write_text("[]")
    pause_issues = tmp_path / "pause-issues.json"
    pause_issues.write_text("[]")
    probe_issues = tmp_path / "probe-issues.json"
    probe_issues.write_text("[]")
    keeper = tmp_path / "keeper.json"
    keeper.write_text('{"body": "", "comments": []}')
    (tmp_path / "comment-bodies.txt").write_text("")

    return {
        "PATH": f"{bindir}:{Path(jq).parent}:/usr/bin:/bin",
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "LIST_CALLS": str(tmp_path / "list-calls"),
        "TIMELINE_JSON": str(timeline),
        "PAUSE_ISSUES_JSON": str(pause_issues),
        "PROBE_ISSUES_JSON": str(probe_issues),
        "KEEPER_JSON": str(keeper),
        "COMMENT_BODIES": str(tmp_path / "comment-bodies.txt"),
        "FAKE_NEW_ISSUE": "42",
        # past=15 puts the base limit at 10 + 15/3 = 15.
        "FAKE_PAST_POSTS": "15",
        "FAKE_TODAY_POSTS": "10",
        "FAKE_RECENT_PRS": "0",
        "FAKE_BOT_ID": str(BOT_ID),
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


def _approve(
    env: dict[str, str],
    *events: dict,
    issue: int = 42,
    labelled_at: str = LABELLED_AT,
) -> None:
    """Put a pause issue on the label, labelled then carrying `events`."""
    Path(env["PAUSE_ISSUES_JSON"]).write_text(
        json.dumps([{"number": issue, "title": PAUSE_TITLE}])
    )
    labelled = {
        "event": "labeled",
        "label": {"name": "tend-rate-limit"},
        "created_at": labelled_at,
    }
    Path(env["TIMELINE_JSON"]).write_text(json.dumps([labelled, *events]))


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


def test_rate_limit_says_so_when_the_issue_cannot_be_filed(
    rate_limit_env: dict[str, str],
) -> None:
    """A failed create must not be reported as a filed issue.

    `set -e` does not reach inside a command substitution, so the failure runs
    on to the function's trailing `printf` and the caller reads success with an
    empty number. The run is refused either way; what is lost is the notice —
    and the annotation used to print a literal `#?`, sending a maintainer after
    an issue that does not exist while the bot stays halted for the UTC day.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    rate_limit_env["FAIL_ISSUE_CREATE"] = "1"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert "could not be filed" in result.stdout
    assert "#?" not in result.stdout


def test_rate_limit_names_the_issue_when_the_index_lags(
    rate_limit_env: dict[str, str],
) -> None:
    """Created while the issue index lagged: still name the number.

    The reconcile reads the number off the create's own URL rather than out of
    a list, so a lagging index no longer costs the annotation its number — the
    state this used to cover (filed, number unknown) is unreachable now. Still
    distinct from a failed create: the issue is there to be closed, so the
    annotation offers the approval route either way.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    # The lag is the subject, so it is set here rather than left to the
    # fixture's default: the create succeeds, and the list it reconciles
    # against still does not show the issue.
    Path(rate_limit_env["PAUSE_ISSUES_JSON"]).write_text("[]")

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert f"#{rate_limit_env['FAKE_NEW_ISSUE']}" in result.stdout
    assert "could not be filed" not in result.stdout
    assert "#?" not in result.stdout


def test_rate_limit_keeps_its_annotation_when_the_row_cannot_be_appended(
    rate_limit_env: dict[str, str],
) -> None:
    """A failed comment must not cost the run its annotation.

    The append path is the common one — every refusal after the first in an
    incident takes it — and a bare pipeline under `set -e` aborts the script on
    it, so the run leaves no trace at all. The row is the lesser loss: the issue
    exists, so the annotation can still say what to close.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    rate_limit_env["FAIL_ISSUE_COMMENT"] = "1"
    # The issue exists and carries the label, but nothing has approved it.
    _approve(rate_limit_env)

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert "Refused runs are listed in #42" in result.stdout


def test_rate_limit_files_nothing_when_the_issue_list_cannot_be_read(
    rate_limit_env: dict[str, str],
) -> None:
    """A failed list read must not be taken for "no issue exists".

    Both readings are the empty string, and acting on the wrong one files a
    second pause issue. The reconcile cannot merge that one away: it probes the
    ten numbers under the issue it just filed, and an already-open pause issue
    is normally far older. The duplicate then costs an approval outright — the
    lookup resolves to the lowest-numbered issue, so a maintainer closing the
    newer one, which is the issue this run's annotation names, approves nothing.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    # An issue is already open; the point is that this run cannot see it.
    _approve(rate_limit_env)
    rate_limit_env["FAIL_ISSUE_LIST_FROM"] = "1"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert not any(c.startswith("issue create") for c in calls), calls
    assert "could not be read" in result.stdout
    assert "could not be filed" not in result.stdout


def test_rate_limit_still_files_when_only_the_re_read_fails(
    rate_limit_env: dict[str, str],
) -> None:
    """A failed re-read must not suppress the file the first read cleared.

    The two reads rule out different things. The first excludes an already-open
    issue of any age, which is the duplicate worth avoiding; the re-read after
    the jitter only narrows the seconds-wide sibling race, and the reconcile's
    downward probe catches that anyway. Holding off here would pause the bot
    with no issue at all — the outcome opening one exists to avoid — and point
    the maintainer at an issue this run's own first read established isn't
    there.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    # Nothing open, so the first read is a clean "none"; only the re-read fails.
    rate_limit_env["FAIL_ISSUE_LIST_FROM"] = "2"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert any(c.startswith("issue create") for c in _calls(rate_limit_env))
    assert "could not be read" not in result.stdout


def test_rate_limit_files_when_only_the_first_read_fails(
    rate_limit_env: dict[str, str],
) -> None:
    """The re-read's verdict counts when the first read never landed.

    The mirror of the case above, and the reason the re-read raises the flag
    rather than merely leaving it alone. Without that raise the run refuses,
    files nothing, and points the maintainer at an open issue the re-read had
    just established isn't there — the same dead end from the other side.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    # Only the first read fails; the re-read comes back clean and empty.
    rate_limit_env["FAIL_ISSUE_LIST_FROM"] = "1"
    rate_limit_env["FAIL_ISSUE_LIST_UNTIL"] = "1"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert any(c.startswith("issue create") for c in _calls(rate_limit_env))
    assert "could not be read" not in result.stdout


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
    _approve(rate_limit_env, _closed_event("tend-agent", actor_id=BOT_ID))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, (
        "the bot approved itself by closing its own pause issue"
    )


def test_rate_limit_renamed_bot_still_cannot_approve(
    rate_limit_env: dict[str, str],
) -> None:
    """A renamed account is still the bot.

    The account is an ordinary user account, so the type check does nothing for
    it and identifying it is the whole control. Matching on a name would fail
    open the moment the account were renamed: an actor matching nothing reads
    as an approving person. Here the close carries an unfamiliar login and the
    bot's id.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(
        rate_limit_env,
        _closed_event("tend-agent-renamed", actor_id=BOT_ID),
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, (
        "a rename let the bot approve itself; the check is matching on a name"
    )


def test_rate_limit_reconciler_keeps_only_what_the_preflight_filed(
    rate_limit_env: dict[str, str],
) -> None:
    """The reconciler nominates its keeper on the anchor's predicate.

    On the label alone, any lower-numbered issue carrying it outranks the
    record just filed, which is then closed as that issue's duplicate — the
    refused-run rows and the `::error::` end up pointing at different issues.
    The reconciler probes numbers below its own one at a time, so the whole
    predicate — author, title, label, still open — runs per issue; each of
    these sits inside the probe window failing exactly one of them.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    Path(rate_limit_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps(
            [
                _probe_issue(
                    41, title="Something a maintainer labelled", label=PAUSE_LABEL
                ),
                _probe_issue(40, title=PAUSE_TITLE, label="unrelated-label"),
                _probe_issue(39, title=PAUSE_TITLE, label=PAUSE_LABEL, login="someone"),
                _probe_issue(38, title=PAUSE_TITLE, label=PAUSE_LABEL, state="closed"),
            ]
        )
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert any(c.startswith("issue create") for c in _calls(rate_limit_env))
    closed = [c for c in _calls(rate_limit_env) if c.startswith("issue close")]
    assert not closed, f"reconciled against issues the preflight never filed: {closed}"
    probes = [
        c
        for c in _calls(rate_limit_env)
        if c.startswith("api repos/owner/repo/issues/")
    ]
    assert probes, "the reconciler never probed"


def test_rate_limit_reconciler_stands_down_to_a_racing_sibling(
    rate_limit_env: dict[str, str],
) -> None:
    """A sibling that filed first keeps the record; this leg closes its own.

    The pair only exists because both legs read the list as empty inside the
    window it takes to reflect a fresh create, so the reconcile cannot re-read
    that list — it probes the numbers below its own, which are primary-key
    reads and return the sibling the instant it exists.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    Path(rate_limit_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps([_probe_issue(41, title=PAUSE_TITLE, label=PAUSE_LABEL)])
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert any(c.startswith("issue close 42") for c in calls), (
        f"both legs kept their own record: {calls}"
    )
    # The `::error::` has to name the survivor, not the issue just closed.
    assert "#41" in result.stdout, result.stdout


def test_rate_limit_carries_its_row_onto_the_racing_sibling(
    rate_limit_env: dict[str, str],
) -> None:
    """Standing down must not strand the refused run's row.

    Here the row *is* the notice: the `::error::` sends the maintainer to the
    survivor, and closing that issue is what lifts the ceiling. So the leg that
    stands down has to move its row across first — otherwise the one artifact a
    person is asked to act on is the one missing the run it refused.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    Path(rate_limit_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps([_probe_issue(41, title=PAUSE_TITLE, label=PAUSE_LABEL)])
    )
    # A sibling from another workflow: its seed row cites a different run.
    Path(rate_limit_env["KEEPER_JSON"]).write_text(
        json.dumps({"body": "run 999 row", "comments": []})
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert any(c.startswith("issue comment 41") for c in calls), (
        f"closed its own record without carrying the row over: {calls}"
    )
    assert any(c.startswith("issue close 42") for c in calls), calls
    assert RUN_LINK in _comments(rate_limit_env), _comments(rate_limit_env)


def test_rate_limit_relabelled_issue_does_not_carry_its_closes(
    rate_limit_env: dict[str, str],
) -> None:
    """Moving the label onto an already-closed issue grants nothing.

    The bot holds `issues: write`, so it can label any issue. Were approvals
    counted from the whole history, labelling one a maintainer had closed
    earlier today would import that close as an approval nobody gave. Only
    closes after the label went on count, and on a real pause issue the label
    goes on at creation, so nothing genuine is excluded.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(
        rate_limit_env,
        _closed_event("maintainer"),
        labelled_at=f"{TODAY}T10:00:00Z",
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "a close predating the label counted as an approval"


def test_rate_limit_skips_the_issue_when_the_burst_limit_refused(
    rate_limit_env: dict[str, str],
) -> None:
    """A burst trip files nothing: closing the issue could not lift it.

    The burst limit is deliberately not resumable, so an issue offering to
    double the ceiling would promise a recovery it cannot deliver.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    rate_limit_env["FAKE_RECENT_PRS"] = "11"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert not any(c.startswith("issue create") for c in calls), (
        f"filed a rate-limit issue for a burst trip it cannot lift: {calls}"
    )


def test_rate_limit_refuses_to_run_without_an_identity(
    rate_limit_env: dict[str, str],
) -> None:
    """Unable to read its own identity, the preflight stops rather than guesses.

    Every count and the approval filter are keyed on who the bot is. Carrying
    on without that would leave the counts matching nothing and the filter
    matching every close — a check that has silently reversed rather than
    failed.
    """
    rate_limit_env["FAIL_WHOAMI"] = "1"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert "could not read the bot's own identity" in result.stdout


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
    """Approvals are scoped to today, since the count they lift resets daily.

    The label is dated a day back too, so the day floor is what excludes this
    close. Left at today's default, the label-ordering rule would exclude it
    first and this test would pass without the floor.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(
        rate_limit_env,
        _closed_event("maintainer", day="2026-01-01"),
        labelled_at="2026-01-01T08:00:00Z",
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "yesterday's approval lifted today's ceiling"


def test_rate_limit_foreign_issue_is_not_the_anchor(
    rate_limit_env: dict[str, str],
) -> None:
    """Only an issue the preflight filed anchors the approval.

    The bot holds `issues: write`, so it can label anything. Were the label the
    whole predicate, the lowest-numbered issue carrying it would be nominated
    and a close on it read as an approval nobody gave. The title half runs
    through the script's real `--jq`; the author half is a server-side flag the
    fake can't apply, so it is asserted on the call the script made.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("maintainer"))
    Path(rate_limit_env["PAUSE_ISSUES_JSON"]).write_text(
        json.dumps([{"number": 7, "title": "Something a maintainer labelled"}])
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "a foreign issue was taken as the anchor"
    # `--state all` is the anchor lookup; the reconciler's own list is
    # `--state open`, and would otherwise satisfy this on its own.
    lookups = [
        c
        for c in _calls(rate_limit_env)
        if c.startswith("issue list") and "--state all" in c
    ]
    assert lookups, "the anchor lookup never ran"
    assert all("--author @me" in c for c in lookups), (
        f"the anchor lookup is not scoped to issues the bot authored: {lookups}"
    )


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


# ---------------------------------------------------------------------------
# review-gate.sh — the tend-review pre-check inlined into generated workflows
# ---------------------------------------------------------------------------

REVIEW_GATE = REPO_ROOT / "generator" / "src" / "tend" / "templates" / "review-gate.sh"

FAKE_GH_REVIEW_GATE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$2" in
  repos/*/pulls/*)
    [ -n "${FAIL_PR:-}" ] && exit 1
    cat "$PR_JSON"
    ;;
  repos/*/commits/*/status\\?per_page=100)
    [ -n "${FAIL_STATUS:-}" ] && exit 1
    cat "$STATUS_JSON"
    ;;
  *)
    exit 1
    ;;
esac
"""


@pytest.fixture
def gate_env(tmp_path: Path) -> dict[str, str]:
    """A fake `gh` on PATH plus the workflow env the gate script reads."""
    bindir = _fake_bin(tmp_path, gh=FAKE_GH_REVIEW_GATE)

    pr = tmp_path / "pr.json"
    pr.write_text(json.dumps({"state": "open", "head": {"sha": "abc123"}}))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"statuses": []}))

    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_REPOSITORY": "owner/repo",
        "PR": "7",
        "EVENT_ACTION": "synchronize",
        "PR_JSON": str(pr),
        "STATUS_JSON": str(status),
    }


def _run_gate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # `bash -e` mirrors the shell GitHub Actions gives a `run:` block.
    return subprocess.run(
        ["bash", "-e", str(REVIEW_GATE)],
        env=env,
        capture_output=True,
        text=True,
    )


def _should_run(env: dict[str, str]) -> str:
    lines = Path(env["GITHUB_OUTPUT"]).read_text().splitlines()
    values = [line.split("=", 1)[1] for line in lines if line.startswith("should_run=")]
    assert len(values) == 1, f"expected exactly one should_run, got: {lines}"
    return values[0]


def _stamp(env: dict[str, str], *statuses: dict[str, str]) -> None:
    Path(env["STATUS_JSON"]).write_text(json.dumps({"statuses": list(statuses)}))


def test_review_gate_skips_a_stamped_head(gate_env: dict[str, str]) -> None:
    """A `synchronize` whose live HEAD carries this PR's stamp is a no-op."""
    _stamp(gate_env, {"context": "tend-review/7", "state": "success"})

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _should_run(gate_env) == "false"


def test_review_gate_runs_when_head_is_unstamped(gate_env: dict[str, str]) -> None:
    """Foreign contexts don't gate: another PR's stamp (one branch can be two
    open PRs with different bases) and a non-success state both leave the
    review to run."""
    _stamp(
        gate_env,
        {"context": "tend-review/8", "state": "success"},
        {"context": "tend-review/7", "state": "pending"},
        {"context": "ci/tests", "state": "success"},
    )

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _should_run(gate_env) == "true"


def test_review_gate_only_gates_synchronize(gate_env: dict[str, str]) -> None:
    """`opened`/`reopened`/`ready_for_review` always run — with no API calls,
    so a GitHub blip can't fail the ungated path."""
    gate_env["EVENT_ACTION"] = "ready_for_review"
    _stamp(gate_env, {"context": "tend-review/7", "state": "success"})

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _should_run(gate_env) == "true"
    assert not Path(gate_env["GH_CALLS"]).exists(), "ungated event still hit the API"


def test_review_gate_skips_closed_prs(gate_env: dict[str, str]) -> None:
    """A queued run whose PR was merged or closed while it waited is a no-op."""
    Path(gate_env["PR_JSON"]).write_text(
        json.dumps({"state": "closed", "head": {"sha": "abc123"}})
    )

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _should_run(gate_env) == "false"


@pytest.mark.parametrize("failure", ["FAIL_PR", "FAIL_STATUS"])
def test_review_gate_fails_open_on_api_errors(
    gate_env: dict[str, str], failure: str
) -> None:
    """An API error must boot the agent, not silently skip the review."""
    _stamp(gate_env, {"context": "tend-review/7", "state": "success"})
    gate_env[failure] = "1"

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _should_run(gate_env) == "true"


def test_review_gate_fails_open_on_an_html_200(gate_env: dict[str, str]) -> None:
    """A GitHub blip can return an HTML error page with a 200: `gh` exits zero
    but the body isn't JSON. The parse must stay inside the fail-open guard —
    unguarded under the run block's `bash -e` it fails the step, skipping the
    whole review (fail-closed)."""
    _stamp(gate_env, {"context": "tend-review/7", "state": "success"})
    Path(gate_env["PR_JSON"]).write_text("<html>oops</html>")

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _should_run(gate_env) == "true"


REPORT_FAILURE = REPO_ROOT / "shared" / "steps" / "report-failure.sh"
RUN_ISSUE_LIB = REPO_ROOT / "shared" / "steps" / "lib" / "run-issue.sh"

OUTAGE_TITLE = "Bot temporarily unavailable"
OUTAGE_LABEL = "tend-outage"

# `gh` stand-in for the outage reporter. Same shape as the rate-limit fake —
# fixtures in, the script's own `--jq` doing the filtering — plus it captures
# comment bodies, which arrive on stdin (`-F -`) rather than in the args.
FAKE_GH_REPORT_FAILURE = r"""#!/usr/bin/env bash
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

case "$1 $2" in
  "api user") emit '{"login":"tend-agent","id":4242}' ;;
  "issue list")
    if [ -n "${FAIL_ISSUE_LIST_FROM:-}" ]; then
      n=$(( $(cat "$LIST_CALLS" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$LIST_CALLS"
      if [ "$n" -ge "$FAIL_ISSUE_LIST_FROM" ]; then exit 1; fi
    fi
    emit "$(cat "$OPEN_ISSUES_JSON")"
    ;;
  "issue create") echo "https://github.com/owner/repo/issues/${FAKE_NEW_ISSUE}" ;;
  "issue view") emit "$(cat "$KEEPER_JSON")" ;;
  "issue comment") cat >> "$COMMENT_BODIES" ;;
  "issue close" | "label create") ;;
  *)
    case "$2" in
      # The reconciler's primary-key probe.
      repos/*/issues/*)
        emit "$(jq -c --argjson n "${2##*/}" \
          'map(select(.number == $n)) | .[0] // {"number":0}' "$PROBE_ISSUES_JSON")"
        ;;
      *) exit 1 ;;
    esac
    ;;
esac
"""


@pytest.fixture
def report_failure_env(tmp_path: Path) -> dict[str, str]:
    """Fake gh/sleep on PATH, plus the Actions env the reporter reads."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in (("gh", FAKE_GH_REPORT_FAILURE), ("sleep", FAKE_SLEEP)):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)

    jq = shutil.which("jq")
    assert jq, "jq is required for these tests"

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 851}}))
    for name in ("open-issues.json", "probe-issues.json"):
        (tmp_path / name).write_text("[]")
    (tmp_path / "keeper.json").write_text('{"body": "", "comments": []}')
    (tmp_path / "comment-bodies.txt").write_text("")

    return {
        "PATH": f"{bindir}:{Path(jq).parent}:/usr/bin:/bin",
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "LIST_CALLS": str(tmp_path / "list-calls"),
        "OPEN_ISSUES_JSON": str(tmp_path / "open-issues.json"),
        "PROBE_ISSUES_JSON": str(tmp_path / "probe-issues.json"),
        "KEEPER_JSON": str(tmp_path / "keeper.json"),
        "COMMENT_BODIES": str(tmp_path / "comment-bodies.txt"),
        "FAKE_NEW_ISSUE": "42",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_EVENT_PATH": str(event),
    }


def _run_report_failure(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REPORT_FAILURE)], env=env, capture_output=True, text=True
    )


def _outage_probe(number: int, **kw) -> dict:
    kw.setdefault("title", OUTAGE_TITLE)
    kw.setdefault("label", OUTAGE_LABEL)
    return _probe_issue(number, **kw)


def test_report_failure_files_when_nothing_is_open(
    report_failure_env: dict[str, str],
) -> None:
    """No open tracker and no racing sibling: file one and keep it."""
    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert any(c.startswith("issue create") for c in calls), calls
    assert not any(c.startswith("issue close") for c in calls), (
        f"closed the tracker it had just filed: {calls}"
    )


def test_report_failure_appends_to_the_open_tracker(
    report_failure_env: dict[str, str],
) -> None:
    """An open tracker takes the row as a comment rather than a second issue."""
    Path(report_failure_env["OPEN_ISSUES_JSON"]).write_text(
        json.dumps([{"number": 8, "title": OUTAGE_TITLE}])
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert any(c.startswith("issue comment 8") for c in calls), calls
    assert not any(c.startswith("issue create") for c in calls), (
        f"filed a second tracker while one was open: {calls}"
    )


def test_report_failure_files_nothing_when_the_issue_list_cannot_be_read(
    report_failure_env: dict[str, str],
) -> None:
    """The same conflation, from the other caller.

    Two open trackers is the state that breaks the drain sweep: later rows
    scatter across both and neither carries the complete set. The reconcile's
    downward probe does not reach an older tracker, so the duplicate persists.
    Skipping costs this one row, and the next failure records normally.
    """
    Path(report_failure_env["OPEN_ISSUES_JSON"]).write_text(
        json.dumps([{"number": 8, "title": OUTAGE_TITLE}])
    )
    report_failure_env["FAIL_ISSUE_LIST_FROM"] = "1"

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert not any(c.startswith("issue create") for c in calls), calls
    assert "::warning::" in result.stdout


def test_report_failure_carries_its_row_onto_the_racing_sibling(
    report_failure_env: dict[str, str],
) -> None:
    """Standing down must not strand the failure it recorded.

    The row lives in the body of the issue this leg filed, so closing that
    issue takes the row with it unless it is carried onto the survivor first.

    Two siblings rather than one, because with a single match "lowest" and
    "nearest" are the same answer and the choice between them goes untested.
    Convergence rests on lowest: a third leg filing #43 sees both #41 and #38,
    and only if every leg keeps descending past the first hit do they agree on
    one keeper instead of scattering rows across two.
    """
    Path(report_failure_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps([_outage_probe(41), _outage_probe(38)])
    )
    # A sibling from another workflow: its seed row cites a different run.
    Path(report_failure_env["KEEPER_JSON"]).write_text(
        json.dumps({"body": "run 999 row", "comments": []})
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert any(c.startswith("issue comment 38") for c in calls), (
        f"carried the row onto the nearest sibling rather than the lowest: {calls}"
    )
    assert not any(c.startswith("issue comment 41") for c in calls), (
        f"stopped at the first hit instead of descending to the lowest: {calls}"
    )
    assert any(c.startswith("issue close 42") for c in calls), calls
    assert "Duplicate of #38" in " ".join(calls), calls
    assert RUN_LINK in _comments(report_failure_env), _comments(report_failure_env)


def test_report_failure_does_not_repeat_a_row_the_keeper_already_has(
    report_failure_env: dict[str, str],
) -> None:
    """Matrix legs share one run id, so the keeper's seed row is already ours."""
    Path(report_failure_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps([_outage_probe(41)])
    )
    Path(report_failure_env["KEEPER_JSON"]).write_text(
        json.dumps({"body": f"| when | {RUN_LINK} | #851 |", "comments": []})
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert any(c.startswith("issue close 42") for c in calls), calls
    assert not any(c.startswith("issue comment") for c in calls), (
        f"repeated a row the keeper already carried: {calls}"
    )


def test_report_failure_does_not_adopt_a_foreign_issue(
    report_failure_env: dict[str, str],
) -> None:
    """The bot holds `issues: write`, so the label alone nominates nothing."""
    Path(report_failure_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps(
            [
                _outage_probe(41, login="someone"),
                _outage_probe(40, title="A maintainer's issue"),
                _outage_probe(39, label="unrelated-label"),
                _outage_probe(38, state="closed"),
            ]
        )
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert not any(c.startswith("issue close") for c in calls), (
        f"stood down to an issue the reporter never filed: {calls}"
    )


def test_report_failure_propagates_a_failed_create(
    report_failure_env: dict[str, str], tmp_path: Path
) -> None:
    """A create that fails must redden the step, not report a phantom issue.

    Under `set -e` the create has to stay in its own assignment: wrapped in
    another command its status would be the wrapper's, and a failed create
    would sail past with an empty issue number and the outage unrecorded.
    """
    gh = Path(report_failure_env["PATH"].split(":")[0]) / "gh"
    gh.write_text(
        FAKE_GH_REPORT_FAILURE.replace(
            '"issue create") echo "https://github.com/owner/repo/issues/${FAKE_NEW_ISSUE}" ;;',
            '"issue create") echo "gh: API error" >&2; exit 1 ;;',
        )
    )
    gh.chmod(0o755)

    result = _run_report_failure(report_failure_env)

    assert result.returncode != 0, (
        f"a failed create left the step green; stdout:\n{result.stdout}"
    )


def test_run_issue_reconcile_refuses_a_call_with_no_row(
    report_failure_env: dict[str, str],
) -> None:
    """Both callers pass a row, so omitting one is a bug, not a mode.

    It has to abort *before* the create: a leg that files an issue and then
    stands down without carrying its row over strands the incident in the
    duplicate it closes, which is the failure the carry-over exists to prevent.
    """
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{RUN_ISSUE_LIB}"'
            f"; run_issue_create_and_reconcile {OUTAGE_LABEL} {OUTAGE_TITLE!r}"
            "; echo REACHED",
        ],
        env=report_failure_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, result.stdout
    assert "REACHED" not in result.stdout, (
        f"ran on past a call with no row: {result.stdout}"
    )
    assert "the row for this run is required" in result.stderr, result.stderr
    calls = Path(report_failure_env["GH_CALLS"])
    assert not calls.exists(), f"reached gh before refusing: {calls.read_text()}"


# ---------------------------------------------------------------------------
# install-claude-binary.sh — the sandbox install, and the CDN blips it rides out
# ---------------------------------------------------------------------------

INSTALL_CLAUDE_BINARY = REPO_ROOT / "shared" / "steps" / "install-claude-binary.sh"

# `sudo -u USER rest…` with the -u dropped: the test has no sandbox user, and
# what is under test is the retry loop rather than the privilege drop.
FAKE_SUDO = """#!/usr/bin/env bash
[ "$1" = "-u" ] && shift 2
exec "$@"
"""

# Fails with the CDN's 403 for the first $CURL_FAILURES attempts, then emits an
# installer that plants a `claude` where the script looks for it. Counting in a
# file rather than a variable because each attempt is a fresh process.
FAKE_CURL = """#!/usr/bin/env bash
n=$(cat "$CURL_ATTEMPTS" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$CURL_ATTEMPTS"
if [ "$n" -le "${CURL_FAILURES:-0}" ]; then
  echo "curl: (22) The requested URL returned error: 403" >&2
  exit 22
fi
cat <<'INSTALLER'
mkdir -p "$HOME/.local/bin"
printf '#!/usr/bin/env bash\\necho claude %s\\n' "$1" > "$HOME/.local/bin/claude"
chmod +x "$HOME/.local/bin/claude"
INSTALLER
"""

# Records what the backoff asked for instead of waiting it out, so a test can
# assert the shape of the delay without paying it. Distinct from the
# discard-only FAKE_SLEEP above, which several fixtures share — same module,
# so reusing that name would rebind theirs.
FAKE_SLEEP_RECORDING = """#!/usr/bin/env bash
printf '%s\\n' "$1" >> "$SLEEPS"
"""


@pytest.fixture
def install_env(tmp_path: Path) -> dict[str, str]:
    """Fake sudo/curl/sleep on PATH plus the env the install step is given."""
    bindir = _fake_bin(
        tmp_path, sudo=FAKE_SUDO, curl=FAKE_CURL, sleep=FAKE_SLEEP_RECORDING
    )
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()

    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "SANDBOX": "tend-sandbox",
        "AGENT_HOME": str(agent_home),
        "CLAUDE_VERSION": "2.1.220",
        "CURL_ATTEMPTS": str(tmp_path / "curl-attempts"),
        "SLEEPS": str(tmp_path / "sleeps"),
    }


def _run_install(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL_CLAUDE_BINARY)],
        env=env,
        capture_output=True,
        text=True,
    )


def _attempts(env: dict[str, str]) -> int:
    return int(Path(env["CURL_ATTEMPTS"]).read_text().strip())


def _sleeps(env: dict[str, str]) -> list[int]:
    path = Path(env["SLEEPS"])
    if not path.exists():
        return []
    return [int(line) for line in path.read_text().split()]


def test_install_claude_binary_rides_out_a_403_burst(
    install_env: dict[str, str],
) -> None:
    """Four straight 403s must not cost the run.

    Observed in production: every attempt inside a ~15s window answered 403
    while sibling matrix legs installed fine seconds either side. Three
    attempts is too few to cross a blip that short, and the step failing this
    early leaves no outage row, so the run vanishes without a trace.
    """
    install_env["CURL_FAILURES"] = "4"

    result = _run_install(install_env)

    assert result.returncode == 0, (
        f"gave up on a blip it should have ridden out; stderr:\n{result.stderr}"
    )
    assert _attempts(install_env) == 5, _attempts(install_env)
    assert (Path(install_env["AGENT_HOME"]) / ".local/bin/claude").exists()


def test_install_claude_binary_backs_off_exponentially(
    install_env: dict[str, str],
) -> None:
    """Each wait at least doubles, so the window spans a blip rather than a burst.

    A flat delay spends every attempt inside the first few seconds, which is
    the failure above. Only the floors are pinned — the jitter above each one
    is deliberate and free to vary.
    """
    install_env["CURL_FAILURES"] = "99"

    _run_install(install_env)

    assert _sleeps(install_env) == pytest.approx([5, 10, 20, 40], abs=9), (
        f"backoff did not double: {_sleeps(install_env)}"
    )
    assert all(
        actual >= floor
        for actual, floor in zip(_sleeps(install_env), [5, 10, 20, 40], strict=True)
    ), f"slept less than the backoff floor: {_sleeps(install_env)}"


def test_install_claude_binary_reddens_when_every_attempt_fails(
    install_env: dict[str, str],
) -> None:
    """A CDN that stays down still fails the step, and says how hard it tried."""
    install_env["CURL_FAILURES"] = "99"

    result = _run_install(install_env)

    assert result.returncode != 0, result.stdout
    assert "after 5 attempts" in result.stdout, result.stdout
    assert _attempts(install_env) == 5, _attempts(install_env)


def test_install_claude_binary_installs_first_try_without_sleeping(
    install_env: dict[str, str],
) -> None:
    """The happy path is one fetch and no delay — the retry must not cost it."""
    result = _run_install(install_env)

    assert result.returncode == 0, result.stderr
    assert _attempts(install_env) == 1, _attempts(install_env)
    assert _sleeps(install_env) == [], _sleeps(install_env)
    assert "claude 2.1.220" in result.stdout, result.stdout
