"""Integration tests exercising the full init/check CLI flow end-to-end.

Unit tests (test_generate.py, test_checks.py, test_config_edge_cases.py) cover
individual functions. These tests run the CLI against a temp directory with a
.config/tend.toml and verify the generated workflow files on disk.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import click.testing
import pytest
import yaml
from click.testing import CliRunner

from tend.checks import CheckResult
from tend.cli import main


def _write_config(tmp_path: Path, content: str) -> None:
    cfg = tmp_path / ".config" / "tend.toml"
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
    _write_config(tmp_path, 'bot_name = "test-bot"')
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
        # Every workflow references the tend composite action
        assert "max-sixty/tend@v1" in path.read_text(), (
            f"{path.name} missing action reference"
        )


def test_init_workflows_have_correct_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each workflow type uses the correct GitHub event trigger."""
    _write_config(
        tmp_path,
        dedent("""\
        bot_name = "test-bot"
        [workflows.ci-fix]
        watched_workflows = ["ci"]
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
        assert trigger in data[True], f"{filename} missing trigger '{trigger}'"


def test_init_workflows_have_required_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All workflows must request contents:write, pull-requests:write, and
    id-token:write for the tend action to function."""
    _write_config(tmp_path, 'bot_name = "test-bot"')
    monkeypatch.chdir(tmp_path)
    _run_init()

    wf_dir = _workflow_dir(tmp_path)
    for path in wf_dir.glob("tend-*.yaml"):
        data = yaml.safe_load(path.read_text())
        for job_name, job in data["jobs"].items():
            if "permissions" not in job:
                continue  # mention's verify job has no permissions block
            perms = job["permissions"]
            assert perms.get("contents") == "write", (
                f"{path.name}:{job_name} missing contents:write"
            )
            assert perms.get("pull-requests") == "write", (
                f"{path.name}:{job_name} missing pull-requests:write"
            )
            assert perms.get("id-token") == "write", (
                f"{path.name}:{job_name} missing id-token:write"
            )


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
        bot_name = "test-bot"
        [workflows.ci-fix]
        watched_workflows = ["build", "test"]
    """),
    )
    monkeypatch.chdir(tmp_path)
    _run_init()

    ci_fix_path = _workflow_dir(tmp_path) / "tend-ci-fix.yaml"
    assert ci_fix_path.exists()
    data = yaml.safe_load(ci_fix_path.read_text())
    workflows_trigger = data[True]["workflow_run"]["workflows"]
    assert "build" in workflows_trigger
    assert "test" in workflows_trigger


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_init_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running init twice produces identical files."""
    _write_config(tmp_path, 'bot_name = "test-bot"')
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
    custom = tmp_path / "custom" / "my-tend.toml"
    custom.parent.mkdir(parents=True)
    custom.write_text('bot_name = "custom-bot"')
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
    _write_config(tmp_path, 'bot_name = "test-bot"')
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


def test_check_passes_repo_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --repo flag is forwarded to run_all_checks."""
    _write_config(tmp_path, 'bot_name = "test-bot"')
    monkeypatch.chdir(tmp_path)

    results = [CheckResult("branch-protection:main", True, "protected")]
    with patch("tend.cli.run_all_checks", return_value=results) as mock_check:
        CliRunner().invoke(main, ["check", "--repo", "owner/repo"])
    mock_check.assert_called_once()
    _, repo_arg = mock_check.call_args.args
    assert repo_arg == "owner/repo"


# ---------------------------------------------------------------------------
# Combined flow: init then check
# ---------------------------------------------------------------------------


def test_init_then_check_combined_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a real user flow: init generates files, then check reports status."""
    _write_config(tmp_path, 'bot_name = "test-bot"')
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
    _write_config(tmp_path, 'bot_name = "test-bot"')
    monkeypatch.chdir(tmp_path)
    _run_init()

    mention = yaml.safe_load(
        (_workflow_dir(tmp_path) / "tend-mention.yaml").read_text()
    )
    assert "verify" in mention["jobs"]
    assert "handle" in mention["jobs"]
    assert mention["jobs"]["handle"]["needs"] == "verify"


# ---------------------------------------------------------------------------
# Bot name flows into workflow content
# ---------------------------------------------------------------------------


def test_init_bot_name_in_workflow_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot_name from config appears in generated workflow files (in the
    tend action's bot_name input and mention filters)."""
    _write_config(tmp_path, 'bot_name = "my-custom-bot"')
    monkeypatch.chdir(tmp_path)
    _run_init()

    for path in _workflow_dir(tmp_path).glob("tend-*.yaml"):
        data = yaml.safe_load(path.read_text())
        for job in data["jobs"].values():
            steps = job.get("steps", [])
            tend_steps = [
                s for s in steps if s.get("uses", "").startswith("max-sixty/tend@")
            ]
            for step in tend_steps:
                assert step["with"]["bot_name"] == "my-custom-bot"
