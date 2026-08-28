"""Tests for the CI-poll scripts in plugins/tend-ci-runner/scripts/.

poll-pr-checks.sh queries a *commit's* rollup, never the PR's — the false
green this design exists to prevent is a poll silently retargeting a head
another actor pushed. The fake `gh` serves raw GraphQL fixtures and the
script's own jq does every reduction, because that filter is the behaviour
under test: which conclusions count as red, which check runs are superseded,
and which never read as green at all. `sleep` is faked, so the 9-iteration
loop runs in milliseconds.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts"
POLL_PR_CHECKS = SCRIPTS / "poll-pr-checks.sh"
RERUN_FAILED_JOBS = SCRIPTS / "rerun-failed-jobs.sh"

HEAD_SHA = "aaaa111122223333aaaa111122223333aaaa1111"

# First-page GraphQL responses are served in sequence: poll N reads
# $ROLLUP_DIR/N.json, falling back to final.json once the sequence runs out.
# A call carrying a cursor is a *later page* of the poll in flight and is
# served from $ROLLUP_DIR/page-<cursor>.json instead — so a page reachable
# only through the cursor is genuinely unreachable without it. The run
# endpoint serves `run_attempt` values consumed line-by-line from $ATTEMPTS
# (the last line repeats), so a rerun's attempt bump is scriptable. `pr view`
# and the jobs endpoints run the script's `--jq` through real jq.
FAKE_GH = (
    GH_PREAMBLE
    + r"""case "$1 $2" in
  "api graphql")
    cursor=""
    for arg in "$@"; do
      case "$arg" in cursor=*) cursor="${arg#cursor=}" ;; esac
    done
    if [ -n "$cursor" ] && [ "$cursor" != "null" ]; then
      cat "$ROLLUP_DIR/page-$cursor.json"
    else
      n=$(( $(cat "$GRAPHQL_CALLS" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$GRAPHQL_CALLS"
      f="$ROLLUP_DIR/$n.json"
      [ -f "$f" ] || f="$ROLLUP_DIR/final.json"
      cat "$f"
    fi
    ;;
  "api repos/owner/repo/commits/"*)
    [ "${COMMIT_EXISTS:-true}" = "true" ]
    ;;
  "pr view")
    emit "$(cat "$HEAD_JSON")"
    ;;
  "run rerun")
    ;;
  api*)
    case "$2" in
      repos/*/actions/runs/*/jobs*)
        emit "$(cat "$JOBS_JSON")"
        ;;
      repos/*/actions/jobs/*)
        emit "$(cat "$JOB_DIR/${2##*/}.json")"
        ;;
      repos/*/actions/runs/*)
        a=$(head -n 1 "$ATTEMPTS")
        if [ "$(wc -l < "$ATTEMPTS")" -gt 1 ]; then
          tail -n +2 "$ATTEMPTS" > "$ATTEMPTS.tmp" && mv "$ATTEMPTS.tmp" "$ATTEMPTS"
        fi
        emit "{\"run_attempt\": $a}"
        ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
"""
)

FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"


def _check_run(
    name: str,
    *,
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    workflow: str = "ci",
    run_id: int = 100,
    started: str | None = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "COMPLETED" else None,
        "startedAt": started,
        "detailsUrl": f"https://github.com/o/r/actions/runs/{run_id}/job/1",
        "checkSuite": {"workflowRun": {"workflow": {"name": workflow}}},
    }


def _status_ctx(context: str, state: str) -> dict:
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": "https://example.com/status",
    }


def _resp(*nodes: dict, has_next: bool = False, end_cursor: str | None = None) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "object": {
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {
                                    "hasNextPage": has_next,
                                    "endCursor": end_cursor,
                                },
                                "nodes": list(nodes),
                            }
                        }
                    }
                }
            }
        }
    )


NULL_ROLLUP = json.dumps(
    {"data": {"repository": {"object": {"statusCheckRollup": None}}}}
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bindir = fake_bin(tmp_path, gh=FAKE_GH, sleep=FAKE_SLEEP)

    rollups = tmp_path / "rollups"
    rollups.mkdir()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (tmp_path / "head.json").write_text(json.dumps({"headRefOid": HEAD_SHA}))
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": []}))
    (tmp_path / "attempts").write_text("1\n")

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GRAPHQL_CALLS": str(tmp_path / "graphql-calls"),
        "ROLLUP_DIR": str(rollups),
        "HEAD_JSON": str(tmp_path / "head.json"),
        "JOBS_JSON": str(tmp_path / "jobs.json"),
        "JOB_DIR": str(jobs_dir),
        "ATTEMPTS": str(tmp_path / "attempts"),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "555",
        "GITHUB_WORKFLOW": "tend-review",
    }


def _serve(env: dict[str, str], *responses: str) -> None:
    """Serve *responses* in order; the last repeats for every later call."""
    for i, resp in enumerate(responses, start=1):
        (Path(env["ROLLUP_DIR"]) / f"{i}.json").write_text(resp)
    (Path(env["ROLLUP_DIR"]) / "final.json").write_text(responses[-1])


def _serve_page(env: dict[str, str], cursor: str, response: str) -> None:
    """Serve *response* to any call that asks for the page after *cursor*."""
    (Path(env["ROLLUP_DIR"]) / f"page-{cursor}.json").write_text(response)


def _poll(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(POLL_PR_CHECKS), "7", HEAD_SHA],
        env=env,
        capture_output=True,
        text=True,
    )


def test_settled_green(env: dict[str, str]) -> None:
    _serve(env, _resp(_check_run("tests"), _status_ctx("codecov/patch", "SUCCESS")))

    result = _poll(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "green" in result.stdout


def test_red_names_the_failing_check_with_its_url(env: dict[str, str]) -> None:
    _serve(env, _resp(_check_run("tests"), _check_run("lint", conclusion="FAILURE")))

    result = _poll(env)

    assert result.returncode == 1
    assert "lint https://github.com/o/r/actions/runs/100/job/1" in result.stdout


@pytest.mark.parametrize(
    "conclusion", ["STARTUP_FAILURE", "ACTION_REQUIRED", "TIMED_OUT"]
)
def test_terminal_non_success_conclusions_count_red(
    env: dict[str, str], conclusion: str
) -> None:
    """A job that never started (STARTUP_FAILURE) or needs action is terminal
    and red — left out of both buckets it would read as green."""
    _serve(env, _resp(_check_run("build", conclusion=conclusion)))

    result = _poll(env)

    assert result.returncode == 1, f"{conclusion} did not read as red"


def test_cancelled_is_not_a_verdict(env: dict[str, str]) -> None:
    _serve(env, _resp(_check_run("tests"), _check_run("old", conclusion="CANCELLED")))

    result = _poll(env)

    assert result.returncode == 0, result.stdout


def test_superseded_failure_yields_to_its_replacement(env: dict[str, str]) -> None:
    """A concurrency-cancelled run's FAILURE stays on the commit forever; only
    the latest check run per (name, workflow) counts. A same-named FAILURE
    from a *different* workflow is not superseded and survives."""
    _serve(
        env,
        _resp(
            _check_run(
                "omnibus",
                conclusion="FAILURE",
                run_id=111,
                started="2026-01-01T00:00:00Z",
            ),
            _check_run(
                "omnibus",
                conclusion="SUCCESS",
                run_id=222,
                started="2026-01-01T00:10:00Z",
            ),
        ),
    )

    assert _poll(env).returncode == 0, "the superseded FAILURE still read as red"

    _serve(
        env,
        _resp(
            _check_run("omnibus", conclusion="SUCCESS", workflow="ci"),
            _check_run("omnibus", conclusion="FAILURE", workflow="nightly", run_id=333),
        ),
    )

    assert _poll(env).returncode == 1, (
        "a same-named FAILURE from a different workflow was wrongly superseded"
    )


def test_queued_replacement_without_startedat_stays_pending(
    env: dict[str, str],
) -> None:
    """A settled FAILURE whose replacement is still QUEUED is unsettled, not
    red. CheckRun.startedAt is nullable and a queued run may not carry one, so
    the group must read pending on any non-terminal entry rather than trusting
    a timestamp race the stale entry would win."""
    _serve(
        env,
        _resp(
            _check_run(
                "tests",
                conclusion="FAILURE",
                run_id=111,
                started="2026-01-01T00:00:00Z",
            ),
            _check_run("tests", status="QUEUED", run_id=222, started=None),
        ),
    )

    result = _poll(env)

    assert result.returncode == 3, "an unsettled FAILURE was reported as a verdict"
    assert "UNVERIFIED" in result.stdout


def test_own_run_and_same_workflow_are_filtered(env: dict[str, str]) -> None:
    """The current run's own check is pending for the whole loop, and a
    sibling run of the same workflow queues behind this run's concurrency
    group — waiting on either deadlocks until the cap."""
    _serve(
        env,
        _resp(
            _check_run("tests"),
            _check_run("review", status="IN_PROGRESS", run_id=555, workflow="x"),
            _check_run("handle", status="QUEUED", workflow="tend-review", run_id=777),
        ),
    )

    result = _poll(env)

    assert result.returncode == 0, result.stdout


def test_pending_status_context_gates(env: dict[str, str]) -> None:
    _serve(env, _resp(_check_run("tests"), _status_ctx("codecov/patch", "PENDING")))

    result = _poll(env)

    assert result.returncode == 3
    assert "codecov/patch" in result.stdout


def test_error_status_context_is_red(env: dict[str, str]) -> None:
    _serve(env, _resp(_status_ctx("ci/legacy", "ERROR")))

    assert _poll(env).returncode == 1


def test_null_rollup_never_reads_green(env: dict[str, str]) -> None:
    """A real commit can have no rollup, which is byte-identical to settled
    green if reduced naively. It must route to UNVERIFIED."""
    _serve(env, NULL_ROLLUP)

    result = _poll(env)

    assert result.returncode == 2
    assert "UNVERIFIED, not green" in result.stdout


def test_unresolvable_oid_is_rejected_before_polling(env: dict[str, str]) -> None:
    env["COMMIT_EXISTS"] = "false"

    result = _poll(env)

    assert result.returncode == 2, result.stdout + result.stderr
    assert f"could not resolve {HEAD_SHA} as a commit in owner/repo" in result.stdout
    assert not Path(env["GRAPHQL_CALLS"]).exists()
    calls = Path(env["GH_CALLS"]).read_text()
    assert calls.count(f"api repos/owner/repo/commits/{HEAD_SHA}") == 2


def test_paginates_past_the_first_page(env: dict[str, str]) -> None:
    """A full matrix registers more than the query's 100-node page — routine
    on a dependency bump, which opens every path filter. The failing check can
    sit on any page, so every page has to survive the walk: a red on the first
    is dropped by an accumulator that lets the last page win, and a red on the
    last is invisible to a query that never follows the cursor."""
    _serve(
        env,
        _resp(
            _check_run("tests"),
            _check_run("build", conclusion="FAILURE", run_id=101),
            has_next=True,
            end_cursor="Y3Vyc29yOjE",
        ),
    )
    _serve_page(env, "Y3Vyc29yOjE", _resp(_check_run("lint", conclusion="FAILURE")))

    result = _poll(env)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "lint https://github.com/o/r/actions/runs/100/job/1" in result.stdout
    assert "build https://github.com/o/r/actions/runs/101/job/1" in result.stdout


def test_truncated_pagination_never_reads_green(env: dict[str, str]) -> None:
    """`hasNextPage` with no cursor to follow leaves the rollup incomplete —
    refetching page one would loop forever, and trusting it could hide a
    failure on a page never read."""
    _serve(env, _resp(_check_run("tests"), has_next=True, end_cursor=None))

    result = _poll(env)

    assert result.returncode == 2
    assert "UNVERIFIED, not green" in result.stdout


def test_cap_report_survives_a_late_api_blip(env: dict[str, str]) -> None:
    """A transient failure on a later iteration must not discard what earlier
    polls saw: the cap report still names the pending checks instead of
    misdiagnosing 'no rollup'."""
    _serve(env, _resp(_check_run("slow-matrix", status="IN_PROGRESS")), NULL_ROLLUP)

    result = _poll(env)

    assert result.returncode == 3
    assert "slow-matrix" in result.stdout


def test_waits_out_pending_then_reports_green(env: dict[str, str]) -> None:
    _serve(
        env,
        _resp(_check_run("tests", status="IN_PROGRESS")),
        _resp(_check_run("tests")),
    )

    result = _poll(env)

    assert result.returncode == 0
    # One poll saw pending, the settle needed the 30s grace re-check: 3 calls.
    assert Path(env["GRAPHQL_CALLS"]).read_text().strip() == "3"


def test_abbreviated_sha_is_rejected_at_entry(env: dict[str, str]) -> None:
    """GraphQL's `GitObjectID!` rejects an abbreviated OID at coercion time, and
    rollup() cannot tell that from a transient failure — so the loop would sleep
    through its whole budget before reporting a caller bug, and head_note's
    string compare would then claim the branch advanced to the very commit that
    was passed in. Reject the argument before any API call."""
    _serve(env, _resp(_check_run("tests")))

    result = subprocess.run(
        [BASH, str(POLL_PR_CHECKS), "7", HEAD_SHA[:7]],
        env=env,
        capture_output=True,
        text=True,
    )
    out = result.stdout + result.stderr

    assert result.returncode == 2, out
    assert "UNVERIFIED, not green" in out
    assert HEAD_SHA[:7] in out
    assert "branch advanced" not in out, "head_note fired on the rejected argument"
    assert not Path(env["GH_CALLS"]).exists(), "the bad argument reached the API"


def test_uppercase_sha_is_rejected_at_entry(env: dict[str, str]) -> None:
    """GraphQL coerces an uppercase OID happily, but head_note compares it
    against `headRefOid`, which comes back lowercase — so an uppercase argument
    would poll its own commit successfully and then trail every verdict with a
    spurious "branch advanced" note pointing at that same commit."""
    _serve(env, _resp(_check_run("tests")))

    result = subprocess.run(
        [BASH, str(POLL_PR_CHECKS), "7", HEAD_SHA.upper()],
        env=env,
        capture_output=True,
        text=True,
    )
    out = result.stdout + result.stderr

    assert result.returncode == 2, out
    assert "UNVERIFIED, not green" in out
    assert "branch advanced" not in out, "head_note fired on the rejected argument"
    assert not Path(env["GH_CALLS"]).exists(), "the bad argument reached the API"


def test_omitted_sha_is_rejected_not_reported_red(env: dict[str, str]) -> None:
    """Omitting <sha> is the likelier arity mistake, and `set -u` would kill the
    script with exit 1 — the code this file documents as red, which sends the
    caller off to diagnose a CI failure that never happened. It has to land on
    the same UNVERIFIED path as any other unusable argument."""
    _serve(env, _resp(_check_run("tests")))

    result = subprocess.run(
        [BASH, str(POLL_PR_CHECKS), "7"],
        env=env,
        capture_output=True,
        text=True,
    )
    out = result.stdout + result.stderr

    assert result.returncode == 2, out
    assert "UNVERIFIED, not green" in out
    assert "unbound variable" not in out
    assert not Path(env["GH_CALLS"]).exists(), "the short call reached the API"


def test_moved_head_is_reported_not_absorbed(env: dict[str, str]) -> None:
    """Another actor pushing while we poll must not retarget the verdict: the
    result stays the pinned SHA's, with the move called out."""
    Path(env["HEAD_JSON"]).write_text(json.dumps({"headRefOid": "b" * 40}))
    _serve(env, _resp(_check_run("tests")))

    result = _poll(env)

    assert result.returncode == 0
    assert "branch advanced to " + "b" * 40 in result.stdout
    assert HEAD_SHA in result.stdout
    assert "pr view 7 --repo owner/repo" in Path(env["GH_CALLS"]).read_text(), (
        "the head re-read must name the repo explicitly"
    )


# --- rerun-failed-jobs.sh ---------------------------------------------------


def _rerun(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(RERUN_FAILED_JOBS), "9000"],
        env=env,
        capture_output=True,
        text=True,
    )


def _attempts(env: dict[str, str], *values: int) -> None:
    Path(env["ATTEMPTS"]).write_text("".join(f"{v}\n" for v in values))


def _jobs_list(env: dict[str, str], *jobs: tuple[int, str, int]) -> None:
    Path(env["JOBS_JSON"]).write_text(
        json.dumps(
            {"jobs": [{"id": i, "status": s, "run_attempt": a} for i, s, a in jobs]}
        )
    )


def _job(
    env: dict[str, str], job_id: int, status: str, conclusion: str, name: str
) -> None:
    (Path(env["JOB_DIR"]) / f"{job_id}.json").write_text(
        json.dumps({"status": status, "conclusion": conclusion, "name": name})
    )


def test_rerun_reports_each_new_attempt_jobs_conclusion(env: dict[str, str]) -> None:
    """`completed` is not `success`: the output names each conclusion, since a
    rerun that failed again is the case the follow-up turns on. Jobs still on
    the prior attempt were not re-run and stay out of the report."""
    _attempts(env, 1, 2)
    _jobs_list(env, (11, "queued", 2), (12, "queued", 2), (13, "completed", 1))
    _job(env, 11, "completed", "success", "lint")
    _job(env, 12, "completed", "failure", "tests")

    result = _rerun(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "success\tlint" in result.stdout
    assert "failure\ttests" in result.stdout
    calls = Path(env["GH_CALLS"]).read_text()
    assert "run rerun 9000 --failed --repo owner/repo" in calls, (
        "the rerun must name the repo explicitly, not lean on cwd remote detection"
    )
    assert "jobs/13" not in calls, "polled a job the rerun never re-queued"


def test_rerun_includes_jobs_that_finished_during_the_wait(
    env: dict[str, str],
) -> None:
    """A fast rerun job can complete before discovery runs. Selecting by
    attempt number still finds it — a status-based scan would read the run as
    'nothing re-queued' and drop the fresh conclusion."""
    _attempts(env, 1, 2)
    _jobs_list(env, (11, "completed", 2))
    _job(env, 11, "completed", "success", "lint")

    result = _rerun(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "success\tlint" in result.stdout


def test_rerun_fails_when_no_attempt_surfaces(env: dict[str, str]) -> None:
    """If `run_attempt` never advances, the rerun did not take; polling the
    old attempt's jobs would report stale conclusions as fresh."""
    _attempts(env, 1)

    result = _rerun(env)

    assert result.returncode == 1
    assert "did not take" in result.stdout


def test_rerun_cap_reports_unverified(env: dict[str, str]) -> None:
    _attempts(env, 1, 2)
    _jobs_list(env, (11, "queued", 2))
    _job(env, 11, "in_progress", "", "tests")

    result = _rerun(env)

    assert result.returncode == 3
    assert "UNVERIFIED" in result.stdout
