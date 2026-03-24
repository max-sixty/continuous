"""Tests for security checks module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tend.checks import (
    CheckResult,
    check_bot_permission,
    check_branch_protection,
    check_secrets,
    detect_repo,
    run_all_checks,
)
from tend.cli import main
from tend.config import Config


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _write_config(tmp_path: Path, content: str = 'bot_name = "test-bot"') -> Path:
    cfg = tmp_path / ".config" / "tend.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(content)
    return cfg


# ---------------------------------------------------------------------------
# detect_repo
# ---------------------------------------------------------------------------


def test_detect_repo_success() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("owner/repo\n")):
        assert detect_repo() == "owner/repo"


def test_detect_repo_failure() -> None:
    with patch("tend.checks._gh", return_value=_make_completed(returncode=1)):
        assert detect_repo() is None


def test_detect_repo_no_gh() -> None:
    with patch("tend.checks._gh", return_value=None):
        assert detect_repo() is None


# ---------------------------------------------------------------------------
# check_branch_protection
# ---------------------------------------------------------------------------


def test_branch_protected() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("true\n")):
        result = check_branch_protection("owner/repo", "main")
    assert result.passed is True
    assert "protected" in result.message


def test_branch_not_protected() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("false\n")):
        result = check_branch_protection("owner/repo", "main")
    assert result.passed is False
    assert "NOT protected" in result.message


def test_branch_protection_api_error() -> None:
    with patch("tend.checks._gh", return_value=_make_completed(returncode=1, stderr="Not Found")):
        result = check_branch_protection("owner/repo", "main")
    assert result.passed is None
    assert "API error" in result.message


def test_branch_protection_no_gh() -> None:
    with patch("tend.checks._gh", return_value=None):
        result = check_branch_protection("owner/repo", "main")
    assert result.passed is None


# ---------------------------------------------------------------------------
# check_bot_permission
# ---------------------------------------------------------------------------


def test_bot_write_permission() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("write\n")):
        result = check_bot_permission("owner/repo", "my-bot")
    assert result.passed is True
    assert "write" in result.message


def test_bot_admin_permission() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("admin\n")):
        result = check_bot_permission("owner/repo", "my-bot")
    assert result.passed is False
    assert "admin" in result.message
    assert "bypass" in result.message


def test_bot_permission_403() -> None:
    with patch("tend.checks._gh", return_value=_make_completed(returncode=1, stderr="HTTP 403")):
        result = check_bot_permission("owner/repo", "my-bot")
    assert result.passed is None
    assert "admin access" in result.message


def test_bot_permission_404_wrong_username() -> None:
    with patch("tend.checks._gh", return_value=_make_completed(returncode=1, stderr="HTTP 404 Not Found")):
        result = check_bot_permission("owner/repo", "typo-bot")
    assert result.passed is None
    assert "not found" in result.message.lower()
    assert "typo-bot" in result.message


# ---------------------------------------------------------------------------
# check_secrets
# ---------------------------------------------------------------------------


def test_secrets_present() -> None:
    with patch("tend.checks._gh", return_value=_make_completed('["BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"]\n')):
        result = check_secrets("owner/repo", ["BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
    assert result.passed is True


def test_secrets_missing() -> None:
    with patch("tend.checks._gh", return_value=_make_completed('["BOT_TOKEN"]\n')):
        result = check_secrets("owner/repo", ["BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
    assert result.passed is False
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.message
    assert "admin:org" not in result.message


def test_secrets_missing_with_org_403_hint() -> None:
    """When org secrets return 403 and secrets are missing, include the hint."""
    with patch("tend.checks._gh", return_value=_make_completed('["BOT_TOKEN"]\n')), \
         patch("tend.checks._list_org_secrets", return_value=(None, True)):
        result = check_secrets("owner/repo", ["BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
    assert result.passed is False
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.message
    assert "admin:org" in result.message
    assert "gh auth refresh" in result.message


def test_secrets_api_error() -> None:
    with patch("tend.checks._gh", return_value=_make_completed(returncode=1, stderr="HTTP 403")):
        result = check_secrets("owner/repo", ["BOT_TOKEN"])
    assert result.passed is None


def test_secrets_bad_json() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("not json")):
        result = check_secrets("owner/repo", ["BOT_TOKEN"])
    assert result.passed is None


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------


def test_run_all_checks_no_gh() -> None:
    with patch("shutil.which", return_value=None):
        results = run_all_checks(Config("bot", "main", "T1", "T2", [], "", {}))
    assert len(results) == 1
    assert results[0].passed is None
    assert "gh CLI" in results[0].message


def test_run_all_checks_no_repo() -> None:
    with patch("shutil.which", return_value="/usr/bin/gh"), \
         patch("tend.checks.detect_repo", return_value=None):
        results = run_all_checks(Config("bot", "main", "T1", "T2", [], "", {}))
    assert len(results) == 1
    assert "detect" in results[0].message


def test_run_all_checks_with_explicit_repo() -> None:
    """Explicit --repo skips auto-detection."""
    def fake_gh(*args: str) -> subprocess.CompletedProcess[str]:
        cmd = args[1] if len(args) > 1 else ""
        if "branches" in cmd:
            return _make_completed("true\n")
        if "collaborators" in cmd:
            return _make_completed("write\n")
        if "secrets" in cmd:
            return _make_completed('["T1","T2"]\n')
        return _make_completed(returncode=1)

    with patch("shutil.which", return_value="/usr/bin/gh"), \
         patch("tend.checks._gh", side_effect=fake_gh):
        results = run_all_checks(Config("bot", "main", "T1", "T2", [], "", {}), repo="owner/repo")
    assert len(results) == 3
    assert all(r.passed is True for r in results)


# ---------------------------------------------------------------------------
# CLI: tend check
# ---------------------------------------------------------------------------


def test_cli_check_all_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    pass_results = [
        CheckResult("branch-protection", True, "protected"),
        CheckResult("bot-permission", True, "write"),
        CheckResult("secrets", True, "present"),
    ]
    with patch("tend.cli.run_all_checks", return_value=pass_results):
        result = CliRunner().invoke(main, ["check"])
    assert result.exit_code == 0
    assert "PASS" in result.output


def test_cli_check_failure_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    results = [
        CheckResult("branch-protection", False, "NOT protected"),
    ]
    with patch("tend.cli.run_all_checks", return_value=results):
        result = CliRunner().invoke(main, ["check"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_cli_check_skips_exit_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All skipped checks should not be treated as failures."""
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    results = [CheckResult("prerequisites", None, "gh not found")]
    with patch("tend.cli.run_all_checks", return_value=results):
        result = CliRunner().invoke(main, ["check"])
    assert result.exit_code == 0
    assert "SKIP" in result.output


# ---------------------------------------------------------------------------
# CLI: init reminder
# ---------------------------------------------------------------------------


def test_init_prints_check_reminder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0
    assert "tend check" in result.output
