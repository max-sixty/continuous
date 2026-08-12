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
from tests import BASH, tool_path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts"
POLL_PR_CHECKS = SCRIPTS / "poll-pr-checks.sh"
POLL_RERUN_JOBS = SCRIPTS / "poll-rerun-jobs.sh"

HEAD_SHA = "aaaa111122223333aaaa111122223333aaaa1111"

# Serves GraphQL responses in sequence: call N reads $ROLLUP_DIR/N.json,
# falling back to final.json once the sequence runs out. `pr view` and the
# jobs endpoints run the script's `--jq` through real jq.
FAKE_GH = r"""#!/usr/bin/env bash
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
  "api graphql")
    n=$(( $(cat "$GRAPHQL_CALLS" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$GRAPHQL_CALLS"
    f="$ROLLUP_DIR/$n.json"
    [ -f "$f" ] || f="$ROLLUP_DIR/final.json"
    cat "$f"
    ;;
  "pr view")
    emit "$(cat "$HEAD_JSON")"
    ;;
  api*)
    case "$2" in
      repos/*/actions/runs/*/jobs*)
        emit "$(cat "$JOBS_JSON")"
        ;;
      repos/*/actions/jobs/*)
        emit "$(cat "$JOB_DIR/${2##*/}.json")"
        ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
"""

FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"


def _check_run(
    name: str,
    *,
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    workflow: str = "ci",
    run_id: int = 100,
    started: str = "2026-01-01T00:00:00Z",
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


def _resp(*nodes: dict) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "object": {
                        "statusCheckRollup": {"contexts": {"nodes": list(nodes)}}
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
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in {"gh": FAKE_GH, "sleep": FAKE_SLEEP}.items():
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)

    rollups = tmp_path / "rollups"
    rollups.mkdir()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (tmp_path / "head.json").write_text(json.dumps({"headRefOid": HEAD_SHA}))
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": []}))

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GRAPHQL_CALLS": str(tmp_path / "graphql-calls"),
        "ROLLUP_DIR": str(rollups),
        "HEAD_JSON": str(tmp_path / "head.json"),
        "JOBS_JSON": str(tmp_path / "jobs.json"),
        "JOB_DIR": str(jobs_dir),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "555",
        "GITHUB_WORKFLOW": "tend-review",
    }


def _serve(env: dict[str, str], *responses: str) -> None:
    """Serve *responses* in order; the last repeats for every later call."""
    for i, resp in enumerate(responses, start=1):
        (Path(env["ROLLUP_DIR"]) / f"{i}.json").write_text(resp)
    (Path(env["ROLLUP_DIR"]) / "final.json").write_text(responses[-1])


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


def test_superseded_failure_with_queued_replacement_stays_pending(
    env: dict[str, str],
) -> None:
    """A settled FAILURE whose replacement is still QUEUED is unsettled, not
    red — `startedAt` is set at queue time, so the replacement wins the
    reduction and lands in `pending`."""
    _serve(
        env,
        _resp(
            _check_run(
                "tests",
                conclusion="FAILURE",
                run_id=111,
                started="2026-01-01T00:00:00Z",
            ),
            _check_run(
                "tests",
                status="QUEUED",
                run_id=222,
                started="2026-01-01T00:10:00Z",
            ),
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
    """A merge-ref commit or unresolvable OID returns a null rollup —
    byte-identical to settled green if reduced naively. It must route to
    UNVERIFIED."""
    _serve(env, NULL_ROLLUP)

    result = _poll(env)

    assert result.returncode == 2
    assert "UNVERIFIED, not green" in result.stdout


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


def test_moved_head_is_reported_not_absorbed(env: dict[str, str]) -> None:
    """Another actor pushing while we poll must not retarget the verdict: the
    result stays the pinned SHA's, with the move called out."""
    Path(env["HEAD_JSON"]).write_text(json.dumps({"headRefOid": "b" * 40}))
    _serve(env, _resp(_check_run("tests")))

    result = _poll(env)

    assert result.returncode == 0
    assert "branch advanced to " + "b" * 40 in result.stdout
    assert HEAD_SHA in result.stdout


# --- poll-rerun-jobs.sh -----------------------------------------------------


def _rerun(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(POLL_RERUN_JOBS), "9000"],
        env=env,
        capture_output=True,
        text=True,
    )


def _jobs_list(env: dict[str, str], *jobs: tuple[int, str]) -> None:
    Path(env["JOBS_JSON"]).write_text(
        json.dumps({"jobs": [{"id": i, "status": s} for i, s in jobs]})
    )


def _job(
    env: dict[str, str], job_id: int, status: str, conclusion: str, name: str
) -> None:
    (Path(env["JOB_DIR"]) / f"{job_id}.json").write_text(
        json.dumps({"status": status, "conclusion": conclusion, "name": name})
    )


def test_rerun_reports_each_jobs_conclusion(env: dict[str, str]) -> None:
    """`completed` is not `success`: the output names each conclusion, since a
    rerun that failed again is the case the follow-up turns on."""
    _jobs_list(env, (11, "queued"), (12, "queued"), (13, "completed"))
    _job(env, 11, "completed", "success", "lint")
    _job(env, 12, "completed", "failure", "tests")

    result = _rerun(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "success\tlint" in result.stdout
    assert "failure\ttests" in result.stdout


def test_rerun_fails_when_no_attempt_surfaced(env: dict[str, str]) -> None:
    """All jobs already `completed` means the rerun never re-queued anything —
    polling would report the *prior* attempt's conclusions as fresh."""
    _jobs_list(env, (11, "completed"))

    result = _rerun(env)

    assert result.returncode == 1
    assert "not re-queued" in result.stdout


def test_rerun_cap_reports_unverified(env: dict[str, str]) -> None:
    _jobs_list(env, (11, "queued"))
    _job(env, 11, "in_progress", "", "tests")

    result = _rerun(env)

    assert result.returncode == 3
    assert "UNVERIFIED" in result.stdout
