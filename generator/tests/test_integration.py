"""Integration tests exercising the full init/check CLI flow end-to-end.

Unit tests (test_generate.py, test_checks.py, test_config_edge_cases.py) cover
individual functions. These tests run the CLI against a temp directory with a
.config/tend.yaml and verify the generated workflow files on disk.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import click.testing
import pytest
from tests import ACTION_VERSION
from tests import _yaml as yaml
from click.testing import CliRunner

from tend.checks import CheckResult
from tend.cli import main


def _write_config(tmp_path: Path, content: str) -> None:
    cfg = tmp_path / ".config" / "tend.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(content)


def _run_init(extra_args: list[str] | None = None) -> click.testing.Result:
    """Run `tend init` via CliRunner. Caller must chdir to the target directory."""
    args = ["init", *(extra_args or [])]
    return CliRunner().invoke(main, args)


def _workflow_dir(tmp_path: Path) -> Path:
    return tmp_path / ".github" / "workflows"


# ---------------------------------------------------------------------------
# Full end-to-end: minimal config → init → verify files on disk
# ---------------------------------------------------------------------------


def test_init_creates_correct_files_with_valid_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minimal config produces 7 workflow files, each valid YAML with expected
    top-level keys (name, on, jobs) and the tend action reference."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    result = _run_init()
    assert result.exit_code == 0

    wf_dir = _workflow_dir(tmp_path)
    files = sorted(p.name for p in wf_dir.glob("tend-*.yaml"))
    assert files == [
        "tend-mention.yaml",
        "tend-nightly.yaml",
        "tend-notifications.yaml",
        "tend-review-runs.yaml",
        "tend-review.yaml",
        "tend-triage.yaml",
        "tend-weekly.yaml",
    ]

    for path in wf_dir.glob("tend-*.yaml"):
        data = yaml.safe_load(path.read_text())
        assert "name" in data, f"{path.name} missing 'name'"
        assert "jobs" in data, f"{path.name} missing 'jobs'"
        assert f"max-sixty/tend/claude@{ACTION_VERSION}" in path.read_text(), (
            f"{path.name} missing action reference"
        )


def test_init_workflows_have_correct_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each workflow type uses the correct GitHub event trigger."""
    _write_config(
        tmp_path,
        dedent("""\
        bot_name: test-bot
        workflows:
          ci-fix:
            watched_workflows: ["ci"]
    """),
    )
    monkeypatch.chdir(tmp_path)
    _run_init()

    wf_dir = _workflow_dir(tmp_path)
    expected_triggers = {
        "tend-review.yaml": "pull_request_target",
        "tend-triage.yaml": "issues",
        "tend-ci-fix.yaml": "workflow_run",
        "tend-nightly.yaml": "schedule",
        "tend-weekly.yaml": "schedule",
        "tend-notifications.yaml": "schedule",
        "tend-review-runs.yaml": "schedule",
    }

    for filename, trigger in expected_triggers.items():
        data = yaml.safe_load((wf_dir / filename).read_text())
        assert trigger in data["on"], f"{filename} missing trigger '{trigger}'"


def test_init_workflows_have_required_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All workflows must request contents:write and pull-requests:write for
    the tend action to function — and must not request id-token:write, which
    nothing in the action chain uses and which would let the most exposed job
    in the repo mint the repository's OIDC identity."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)
    _run_init()

    wf_dir = _workflow_dir(tmp_path)
    checked = 0
    for path in wf_dir.glob("tend-*.yaml"):
        data = yaml.safe_load(path.read_text())
        for job_name, job in data["jobs"].items():
            # The invariant binds the jobs that run the agent; mention's
            # verify job has no permissions block, and its relay job requests
            # only the contents: write its dispatch POST needs — no secrets,
            # no agent.
            if not any(
                s.get("uses", "").startswith("max-sixty/tend/")
                for s in job.get("steps", [])
            ):
                continue
            checked += 1
            perms = job["permissions"]
            assert perms.get("contents") == "write", (
                f"{path.name}:{job_name} missing contents:write"
            )
            assert perms.get("pull-requests") == "write", (
                f"{path.name}:{job_name} missing pull-requests:write"
            )
            assert "id-token" not in perms, (
                f"{path.name}:{job_name} grants id-token, which tend does not use"
            )
    assert checked == 7, "every workflow must contribute one agent job"


# ---------------------------------------------------------------------------
# Config options flow through to generated files on disk
# ---------------------------------------------------------------------------


def test_init_ci_fix_with_watched_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ci-fix workflow is generated when watched_workflows is configured,
    and the watched workflow names appear in the trigger."""
    _write_config(
        tmp_path,
        dedent("""\
        bot_name: test-bot
        workflows:
          ci-fix:
            watched_workflows: ["build", "test"]
    """),
    )
    monkeypatch.chdir(tmp_path)
    _run_init()

    ci_fix_path = _workflow_dir(tmp_path) / "tend-ci-fix.yaml"
    assert ci_fix_path.exists()
    data = yaml.safe_load(ci_fix_path.read_text())
    workflows_trigger = data["on"]["workflow_run"]["workflows"]
    assert "build" in workflows_trigger
    assert "test" in workflows_trigger


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_init_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running init twice produces identical files."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    _run_init()
    first_run = {
        p.name: p.read_text() for p in _workflow_dir(tmp_path).glob("tend-*.yaml")
    }

    _run_init()
    second_run = {
        p.name: p.read_text() for p in _workflow_dir(tmp_path).glob("tend-*.yaml")
    }

    assert first_run == second_run


# ---------------------------------------------------------------------------
# Custom config path
# ---------------------------------------------------------------------------


def test_init_custom_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The -c flag uses a config at a non-default path."""
    custom = tmp_path / "custom" / "my-tend.yaml"
    custom.parent.mkdir(parents=True)
    custom.write_text("bot_name: custom-bot")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(main, ["init", "-c", str(custom)])
    assert result.exit_code == 0

    for path in _workflow_dir(tmp_path).glob("tend-*.yaml"):
        assert "custom-bot" in path.read_text(), f"{path.name} missing custom bot name"


# ---------------------------------------------------------------------------
# Generated header
# ---------------------------------------------------------------------------


def test_init_files_have_generation_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every generated file starts with the 'Generated by tend' header."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)
    _run_init()

    for path in _workflow_dir(tmp_path).glob("tend-*.yaml"):
        content = path.read_text()
        assert content.startswith("# Generated by tend"), (
            f"{path.name} missing generation header"
        )
        assert "Do not edit this file directly" in content


# ---------------------------------------------------------------------------
# tend check — CLI integration with mocked API
# ---------------------------------------------------------------------------


def test_init_warns_when_canonical_owner_undetected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a detectable canonical owner, `init` emits a warning so the
    user can fix their gh setup before shipping un-guarded workflows."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("tend.cli.detect_canonical_owner", lambda: None)

    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0
    assert "could not detect the canonical repo owner" in result.output


def test_init_wires_detected_owner_into_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: cli.init must inject `detect_canonical_owner`'s result
    into the rendered workflows. The per-file rendered shape (all 6 guarded
    workflows, with/without a setup step) is already snapshotted by
    `test_fork_guard_rendered_shape_regtest`; here we only verify the wiring."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("tend.cli.detect_canonical_owner", lambda: "PRQL")

    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0
    content = (_workflow_dir(tmp_path) / "tend-nightly.yaml").read_text()
    assert "github.repository_owner == 'PRQL'" in content


def test_check_passes_repo_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --repo flag is forwarded to run_all_checks."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    results = [CheckResult("branch-protection:main", True, "protected")]
    with patch("tend.cli.run_all_checks", return_value=results) as mock_check:
        CliRunner().invoke(main, ["check", "--repo", "owner/repo"])
    mock_check.assert_called_once()
    _, repo_arg = mock_check.call_args.args
    assert repo_arg == "owner/repo"


def _make_completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _fake_gh_all_pass(*args: str, **kwargs: str) -> subprocess.CompletedProcess[str]:
    """Simulate a gh CLI where all checks pass for owner/repo."""
    if "graphql" in args:
        # The workflow tree the credential sweep reads: no workflows, so no
        # OIDC job and no trigger the bot steers.
        return _make_completed(
            json.dumps({"data": {"repository": {"object": {"entries": []}}}})
        )
    url = next(a for a in args if a.startswith("repos/") or a.startswith("orgs/"))
    # Only the ref-gated environment exists, holding the operational secrets.
    if url.endswith("/environments"):
        return _make_completed("tend\n")
    if url.endswith("/secrets") and "/environments/" in url:
        names = (
            ["TEND_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
            if url.endswith("tend/secrets")
            else []
        )
        if any(a.startswith("[.secrets") for a in args):
            return _make_completed(json.dumps(names))
        return _make_completed("\n".join(names) + "\n")
    if url == "repos/owner/repo" and ".default_branch" in args:
        return _make_completed("main\n")
    if "rules/branches" in url:
        return _make_completed(
            json.dumps(
                [
                    {
                        "type": "update",
                        "ruleset_id": 1,
                        "ruleset_source_type": "Repository",
                        "ruleset_source": "owner/repo",
                    }
                ]
            )
        )
    if "/rulesets/" in url:
        # Admin-only bypass — the shape `tend check --fix` creates.
        return _make_completed(
            json.dumps(
                {
                    "bypass_actors": [
                        {
                            "actor_id": 5,
                            "actor_type": "RepositoryRole",
                            "bypass_mode": "exempt",
                        }
                    ]
                }
            )
        )
    if "branches" in url:
        return _make_completed("true\n")
    if "collaborators" in url:
        return _make_completed(
            json.dumps(
                {
                    "permission": "write",
                    "role_name": "write",
                    "user": {
                        "permissions": {"admin": False, "maintain": False, "push": True}
                    },
                }
            )
        )
    if url.endswith("deployment-branch-policies"):
        return _make_completed('{"name": "main"}\n')
    if url.endswith("environments/tend"):
        return _make_completed(
            json.dumps(
                {
                    "deployment_branch_policy": {
                        "protected_branches": False,
                        "custom_branch_policies": True,
                    }
                }
            )
        )
    # Repo and org level answer bare — the environment-secrets branch above is
    # deliberately the only place the operational names appear, so a check
    # reading the wrong level cannot pass by accident.
    if "secrets" in url:
        return _make_completed("[]\n")
    return _make_completed(returncode=1)


def test_check_full_pipeline_with_mocked_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the full check pipeline: CLI → run_all_checks → individual
    check functions → mocked _gh. Verifies wiring between cli.py and checks.py."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        result = CliRunner().invoke(main, ["check", "--repo", "owner/repo"])

    assert result.exit_code == 0, result.output
    assert "FAIL" not in result.output
    # branch-protection + bot-permission + environment + environment-deployments
    # + credential-environments + secrets + claude-auth + allowlist = 8
    assert result.output.count("PASS") == 8


def test_check_full_pipeline_branch_not_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing branch protection check propagates through to CLI exit code 1."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    def fake_gh_unprotected(
        *args: str, **kwargs: str
    ) -> subprocess.CompletedProcess[str]:
        url = args[1]
        if url == "repos/owner/repo" and ".default_branch" in args:
            return _make_completed("main\n")
        if "rules/branches" in url:
            return _make_completed(json.dumps([]))
        if "branches/main" in url and ".protected" in args:
            return _make_completed("false\n")
        if "collaborators" in url:
            return _make_completed("write\n")
        if "secrets" in url:
            return _make_completed('["TEND_BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"]\n')
        return _make_completed(returncode=1)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=fake_gh_unprotected),
    ):
        result = CliRunner().invoke(main, ["check", "--repo", "owner/repo"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "NOT protected" in result.output


# ---------------------------------------------------------------------------
# Combined flow: init then check
# ---------------------------------------------------------------------------


def test_init_then_check_combined_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a real user flow: init generates files, then check reports status."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()

    # Step 1: init
    init_result = runner.invoke(main, ["init"])
    assert init_result.exit_code == 0
    assert "Generated 7 workflow files" in init_result.output
    assert "tend check" in init_result.output  # reminder to run check

    # Step 2: check (mocked)
    results = [
        CheckResult("branch-protection:main", True, "protected"),
        CheckResult("bot-permission", True, "write"),
        CheckResult("secrets", True, "present"),
    ]
    with patch("tend.cli.run_all_checks", return_value=results):
        check_result = runner.invoke(main, ["check"])
    assert check_result.exit_code == 0


# ---------------------------------------------------------------------------
# Mention workflow specifics (complex multi-job workflow)
# ---------------------------------------------------------------------------


def test_init_mention_workflow_has_two_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mention workflow has verify and handle jobs with proper dependency."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)
    _run_init()

    mention = yaml.safe_load(
        (_workflow_dir(tmp_path) / "tend-mention.yaml").read_text()
    )
    assert "verify" in mention["jobs"]
    assert "handle" in mention["jobs"]
    assert mention["jobs"]["handle"]["needs"] == "verify"


# ---------------------------------------------------------------------------
# Notifications pre-check
# ---------------------------------------------------------------------------


def test_init_notifications_has_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notifications workflow checks for unread notifications before
    invoking Claude, and skips all subsequent steps when count is 0."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)
    _run_init()

    data = yaml.safe_load(
        (_workflow_dir(tmp_path) / "tend-notifications.yaml").read_text()
    )
    steps = data["jobs"]["notifications"]["steps"]

    # First step is the pre-check
    check_step = steps[0]
    assert check_step["id"] == "check"
    assert "gh api notifications" in check_step["run"]

    # All subsequent steps are gated on the check output
    for step in steps[1:]:
        assert "if" in step, (
            f"step {step.get('uses', step.get('name'))} missing if guard"
        )
        assert "steps.check.outputs.count" in step["if"]
        # workflow_dispatch bypasses the pre-check
        assert "workflow_dispatch" in step["if"]


def test_notifications_precheck_tolerates_transient_non_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check must not fail the build when `gh api notifications`
    transiently returns a non-JSON (HTML) body — a routine GitHub API blip.
    The step runs under `bash -e`; an untolerated fetch aborts the whole job
    red. The correct disposition is to skip this cycle (count=0, exit 0); the
    next scheduled cycle picks up anything missed."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)
    _run_init()

    data = yaml.safe_load(
        (_workflow_dir(tmp_path) / "tend-notifications.yaml").read_text()
    )
    script = data["jobs"]["notifications"]["steps"][0]["run"]

    # Fake `gh` mimics a transient GitHub blip: bare `gh api notifications`
    # returns a 200 with an HTML body (exit 0, non-JSON output); the same call
    # with `--jq` fails because gh runs jq internally and jq can't parse HTML.
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do [ "$a" = "--jq" ] && exit 4; done\n'
        'echo "<html><body>error</body></html>"\n'
        "exit 0\n"
    )
    gh.chmod(0o755)
    # No-op sleep so the retry loop's backoff doesn't slow the test.
    sleep = bindir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)

    output_file = tmp_path / "gh_output"
    output_file.write_text("")
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GITHUB_OUTPUT": str(output_file),
        "GITHUB_REPOSITORY": "owner/repo",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"pre-check aborted on a transient non-JSON response "
        f"(exit {result.returncode}); stderr:\n{result.stderr}"
    )
    assert "count=0" in output_file.read_text()


# ---------------------------------------------------------------------------
# Bot name flows into workflow content
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# --with-install-test flag + cleanup of stale tend-*.yaml files
# ---------------------------------------------------------------------------


def test_init_with_install_test_generates_extra_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag adds tend-install-test.yaml on top of the standard set."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    result = _run_init(["--with-install-test"])
    assert result.exit_code == 0

    wf_dir = _workflow_dir(tmp_path)
    files = sorted(p.name for p in wf_dir.glob("tend-*.yaml"))
    assert "tend-install-test.yaml" in files
    assert len(files) == 8  # 7 agent workflows + install-test


def test_init_without_flag_omits_install_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain `init` produces the standard 7-file set without the install-test workflow."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    _run_init()

    wf_dir = _workflow_dir(tmp_path)
    files = sorted(p.name for p in wf_dir.glob("tend-*.yaml"))
    assert "tend-install-test.yaml" not in files


def test_init_removes_install_test_on_regen_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run with flag, then without — the install-test file must disappear.

    This is the lifecycle the install skill depends on: maintainer runs
    `init --with-install-test`, install PR merges, nightly regen runs `init`
    (no flag) and removes the file from the default branch.
    """
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    _run_init(["--with-install-test"])
    install_test = _workflow_dir(tmp_path) / "tend-install-test.yaml"
    assert install_test.exists()

    result = _run_init()
    assert result.exit_code == 0
    assert not install_test.exists()
    assert "removed" in result.output


def test_init_removes_unknown_tend_yaml_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup also removes stale tend-*.yaml files left by older generators
    (renamed workflows, disabled workflows). Non-tend workflows are kept."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    wf_dir = _workflow_dir(tmp_path)
    wf_dir.mkdir(parents=True)
    (wf_dir / "tend-defunct.yaml").write_text("# leftover from an older generator\n")
    (wf_dir / "ci.yaml").write_text("# adopter-owned, must not be touched\n")

    _run_init()

    assert not (wf_dir / "tend-defunct.yaml").exists()
    assert (wf_dir / "ci.yaml").exists()


def test_init_removes_stale_files_when_no_workflows_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling every workflow shouldn't skip cleanup. Stale tend-*.yaml
    must still be removed so a maintainer can confidently turn tend off."""
    _write_config(
        tmp_path,
        dedent("""\
            bot_name: test-bot
            workflows:
              review:
                enabled: false
              mention:
                enabled: false
              triage:
                enabled: false
              nightly:
                enabled: false
              weekly:
                enabled: false
              notifications:
                enabled: false
              review-runs:
                enabled: false
        """),
    )
    monkeypatch.chdir(tmp_path)

    wf_dir = _workflow_dir(tmp_path)
    wf_dir.mkdir(parents=True)
    stale = wf_dir / "tend-review.yaml"
    stale.write_text("# leftover from a previous run\n")

    result = _run_init()
    assert result.exit_code == 0
    assert not stale.exists()
    assert "Removed 1 stale" in result.output


def test_init_dry_run_previews_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run must not delete anything on disk, but should report what
    would be removed so the user can preview the regen accurately."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)

    wf_dir = _workflow_dir(tmp_path)
    wf_dir.mkdir(parents=True)
    stale = wf_dir / "tend-defunct.yaml"
    stale.write_text("# would be removed on a non-dry-run\n")

    result = _run_init(["--dry-run"])

    assert stale.exists()
    assert "would remove" in result.output


def test_install_test_workflow_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install-test workflow has the expected trigger, fork-PR skip, and
    generator-drift check — and reads no secrets. It runs on `pull_request`,
    whose merge ref the `tend` environment's policy refuses, so the secrets
    are unreachable from here by design; `tend check` (run by the installer
    with admin credentials) is what verifies them."""
    _write_config(tmp_path, "bot_name: test-bot")
    monkeypatch.chdir(tmp_path)
    _run_init(["--with-install-test"])

    path = _workflow_dir(tmp_path) / "tend-install-test.yaml"
    content = path.read_text()
    data = yaml.safe_load(content)

    assert data["name"] == "tend-install-test"
    assert "pull_request" in data["on"]
    assert data["on"]["pull_request"]["paths"] == [
        ".github/workflows/tend-*.yaml",
        ".config/tend.yaml",
    ]

    job = data["jobs"]["install-test"]
    assert "head.repo.full_name == github.repository" in job["if"]
    assert job["permissions"] == {"contents": "read"}
    assert "secrets." not in content

    # Generator-drift step regenerates with the same flag to keep output stable.
    # Version is pinned from the committed header (not `@latest`) so a release
    # mid-PR doesn't fail the drift check for an irrelevant reason.
    assert 'uvx "tend@$TEND_VERSION" init --with-install-test' in content
    # Version-agnostic: the exact pin is covered by the regtest output, and
    # weekly bumps shouldn't have to edit two places.
    assert "astral-sh/setup-uv@" in content

    # Default-branch probe: must not use `git remote set-head origin --auto`,
    # which errors on the default shallow `actions/checkout@v7` (only the PR
    # head ref is fetched, so `refs/remotes/origin/<default>` doesn't exist
    # locally). Query the API and fetch the default branch instead. See #582.
    assert "git remote set-head" not in content
    assert "gh api" in content and ".default_branch" in content
    assert "git symbolic-ref" in content


def test_init_bot_name_in_workflow_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot_name from config appears in generated workflow files (in the
    tend action's bot_name input and mention filters)."""
    _write_config(tmp_path, "bot_name: my-custom-bot")
    monkeypatch.chdir(tmp_path)
    _run_init()

    for path in _workflow_dir(tmp_path).glob("tend-*.yaml"):
        data = yaml.safe_load(path.read_text())
        for job in data["jobs"].values():
            steps = job.get("steps", [])
            tend_steps = [
                s
                for s in steps
                if s.get("uses", "").startswith("max-sixty/tend/claude@")
            ]
            for step in tend_steps:
                assert step["with"]["bot_name"] == "my-custom-bot"
