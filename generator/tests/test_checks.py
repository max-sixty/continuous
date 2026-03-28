"""Tests for security checks module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tend.checks import (
    CheckResult,
    _has_restrict_updates_ruleset,
    _ref_matches_patterns,
    _restrict_updates_ruleset,
    check_bot_permission,
    check_branch_protection,
    check_repo_secret_allowlist,
    check_secrets,
    detect_repo,
    run_all_checks,
)
from tend.cli import main
from tend.config import Config


def _make_completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _write_config(
    tmp_path: Path, content: str = 'bot_name = "test-bot"\nmode = "write"'
) -> Path:
    cfg = tmp_path / ".config" / "tend.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(content)
    return cfg


def _make_ruleset(include: list[str], exclude: list[str] | None = None) -> str:
    """Build a JSON array with one active update ruleset targeting given patterns."""
    return json.dumps(
        [
            {
                "enforcement": "active",
                "target": "branch",
                "rules": [{"type": "update"}],
                "conditions": {
                    "ref_name": {
                        "include": include,
                        "exclude": exclude or [],
                    }
                },
            }
        ]
    )


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
    ruleset = _make_ruleset(["~DEFAULT_BRANCH"])

    def fake_gh(*args, **kwargs):
        cmd = " ".join(args)
        if "rulesets" in cmd:
            return _make_completed(ruleset)
        return _make_completed("true\n")

    with patch("tend.checks._gh", side_effect=fake_gh):
        result = check_branch_protection("owner/repo", "main", default_branch="main")
    assert result.passed is True
    assert "protected" in result.message


def test_branch_not_protected() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("false\n")):
        result = check_branch_protection("owner/repo", "main")
    assert result.passed is False
    assert "NOT protected" in result.message


def test_branch_protection_api_error() -> None:
    with patch(
        "tend.checks._gh",
        return_value=_make_completed(returncode=1, stderr="Not Found"),
    ):
        result = check_branch_protection("owner/repo", "main")
    assert result.passed is None
    assert "API error" in result.message


def test_branch_protection_no_gh() -> None:
    with patch("tend.checks._gh", return_value=None):
        result = check_branch_protection("owner/repo", "main")
    assert result.passed is None


def test_branch_protection_result_name_includes_branch() -> None:
    """Each branch gets a distinct check name for identification."""
    with patch("tend.checks._gh", return_value=_make_completed("false\n")):
        main_result = check_branch_protection("owner/repo", "main")
        v1_result = check_branch_protection("owner/repo", "v1")
    assert main_result.name == "branch-protection:main"
    assert v1_result.name == "branch-protection:v1"


# ---------------------------------------------------------------------------
# _ref_matches_patterns
# ---------------------------------------------------------------------------


def test_ref_matches_exact() -> None:
    assert _ref_matches_patterns("refs/heads/v1", ["refs/heads/v1"], None) is True


def test_ref_no_match() -> None:
    assert _ref_matches_patterns("refs/heads/v1", ["refs/heads/main"], None) is False


def test_ref_matches_default_branch_macro() -> None:
    assert _ref_matches_patterns("refs/heads/main", ["~DEFAULT_BRANCH"], "main") is True


def test_ref_default_branch_macro_no_match() -> None:
    """~DEFAULT_BRANCH should not match non-default branches."""
    assert _ref_matches_patterns("refs/heads/v1", ["~DEFAULT_BRANCH"], "main") is False


def test_ref_matches_all_macro() -> None:
    assert _ref_matches_patterns("refs/heads/anything", ["~ALL"], None) is True


def test_ref_matches_glob() -> None:
    assert (
        _ref_matches_patterns("refs/heads/release/v1", ["refs/heads/release/*"], None)
        is True
    )


# ---------------------------------------------------------------------------
# _has_restrict_updates_ruleset
# ---------------------------------------------------------------------------


def test_non_update_ruleset_is_not_detected() -> None:
    """An empty jq result (no matching rulesets) should yield False."""
    with patch("tend.checks._gh", return_value=_make_completed("[]\n")):
        assert _has_restrict_updates_ruleset("owner/repo", "main") is False


def test_update_ruleset_covering_default_branch() -> None:
    """A ruleset targeting ~DEFAULT_BRANCH should match when checking the default branch."""
    data = _make_ruleset(["~DEFAULT_BRANCH"])
    with patch("tend.checks._gh", return_value=_make_completed(data)):
        assert (
            _has_restrict_updates_ruleset("owner/repo", "main", default_branch="main")
            is True
        )


def test_update_ruleset_not_covering_branch() -> None:
    """A ruleset targeting only ~DEFAULT_BRANCH should NOT match a non-default branch."""
    data = _make_ruleset(["~DEFAULT_BRANCH"])
    with patch("tend.checks._gh", return_value=_make_completed(data)):
        assert (
            _has_restrict_updates_ruleset("owner/repo", "v1", default_branch="main")
            is False
        )


def test_update_ruleset_with_explicit_branch() -> None:
    """A ruleset targeting refs/heads/v1 should match v1."""
    data = _make_ruleset(["~DEFAULT_BRANCH", "refs/heads/v1"])
    with patch("tend.checks._gh", return_value=_make_completed(data)):
        assert (
            _has_restrict_updates_ruleset("owner/repo", "v1", default_branch="main")
            is True
        )


def test_update_ruleset_excluded_branch() -> None:
    """A branch in the exclude list should not match, even if include covers it."""
    data = _make_ruleset(["~ALL"], exclude=["refs/heads/v1"])
    with patch("tend.checks._gh", return_value=_make_completed(data)):
        assert (
            _has_restrict_updates_ruleset("owner/repo", "v1", default_branch="main")
            is False
        )


# ---------------------------------------------------------------------------
# _restrict_updates_ruleset
# ---------------------------------------------------------------------------


def test_ruleset_default_branch_only() -> None:
    """No extra branches — ruleset targets only ~DEFAULT_BRANCH."""
    body = json.loads(_restrict_updates_ruleset([]))
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]


def test_ruleset_with_extra_branches() -> None:
    """Extra branches are added as refs/heads/<name> patterns."""
    body = json.loads(_restrict_updates_ruleset(["v1", "v2"]))
    assert body["conditions"]["ref_name"]["include"] == [
        "~DEFAULT_BRANCH",
        "refs/heads/v1",
        "refs/heads/v2",
    ]


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
    with patch(
        "tend.checks._gh", return_value=_make_completed(returncode=1, stderr="HTTP 403")
    ):
        result = check_bot_permission("owner/repo", "my-bot")
    assert result.passed is None
    assert "admin access" in result.message


def test_bot_permission_404_wrong_username() -> None:
    with patch(
        "tend.checks._gh",
        return_value=_make_completed(returncode=1, stderr="HTTP 404 Not Found"),
    ):
        result = check_bot_permission("owner/repo", "typo-bot")
    assert result.passed is None
    assert "not found" in result.message.lower()
    assert "typo-bot" in result.message


# ---------------------------------------------------------------------------
# check_secrets
# ---------------------------------------------------------------------------


def test_secrets_present() -> None:
    with patch(
        "tend.checks._gh",
        return_value=_make_completed('["BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"]\n'),
    ):
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
    with (
        patch("tend.checks._gh", return_value=_make_completed('["BOT_TOKEN"]\n')),
        patch("tend.checks._list_org_secrets", return_value=(None, True)),
    ):
        result = check_secrets("owner/repo", ["BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
    assert result.passed is False
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.message
    assert "admin:org" in result.message
    assert "gh auth refresh" in result.message


def test_secrets_api_error() -> None:
    with patch(
        "tend.checks._gh", return_value=_make_completed(returncode=1, stderr="HTTP 403")
    ):
        result = check_secrets("owner/repo", ["BOT_TOKEN"])
    assert result.passed is None


def test_secrets_bad_json() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("not json")):
        result = check_secrets("owner/repo", ["BOT_TOKEN"])
    assert result.passed is None


# ---------------------------------------------------------------------------
# check_repo_secret_allowlist
# ---------------------------------------------------------------------------


def test_repo_secret_allowlist_pass() -> None:
    """Only allowed secrets at repo level, no org secrets — passes."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed('["BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"]\n'),
        ),
        patch("tend.checks._list_org_secrets", return_value=(set(), False)),
    ):
        result = check_repo_secret_allowlist(
            "owner/repo", {"BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
        )
    assert result.passed is True
    assert "in allowlist" in result.message


def test_repo_secret_allowlist_unexpected_repo() -> None:
    """Unexpected secret at repo level — fails with repo-level annotation."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed(
                '["BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN","PYPI_TOKEN"]\n'
            ),
        ),
        patch("tend.checks._list_org_secrets", return_value=(set(), False)),
    ):
        result = check_repo_secret_allowlist(
            "owner/repo", {"BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
        )
    assert result.passed is False
    assert "PYPI_TOKEN" in result.message
    assert "repo-level" in result.message


def test_repo_secret_allowlist_unexpected_org() -> None:
    """Unexpected secret at org level — fails with org-level annotation."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed('["BOT_TOKEN"]\n'),
        ),
        patch(
            "tend.checks._list_org_secrets",
            return_value=({"BOT_TOKEN", "NPM_TOKEN"}, False),
        ),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"BOT_TOKEN"})
    assert result.passed is False
    assert "NPM_TOKEN" in result.message
    assert "org-level" in result.message


def test_repo_secret_allowlist_unexpected_both() -> None:
    """Unexpected secrets at both levels — message includes both annotations."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed('["BOT_TOKEN","PYPI_TOKEN"]\n'),
        ),
        patch(
            "tend.checks._list_org_secrets",
            return_value=({"NPM_TOKEN"}, False),
        ),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"BOT_TOKEN"})
    assert result.passed is False
    assert "repo-level" in result.message
    assert "org-level" in result.message
    assert "PYPI_TOKEN" in result.message
    assert "NPM_TOKEN" in result.message


def test_repo_secret_allowlist_org_allowed() -> None:
    """Org-level secret in the allowlist — passes."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed('["BOT_TOKEN"]\n'),
        ),
        patch(
            "tend.checks._list_org_secrets",
            return_value=({"CODECOV_TOKEN"}, False),
        ),
    ):
        result = check_repo_secret_allowlist(
            "owner/repo", {"BOT_TOKEN", "CODECOV_TOKEN"}
        )
    assert result.passed is True


def test_repo_secret_allowlist_org_forbidden() -> None:
    """Org secrets return 403 — passes but notes the gap."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed('["BOT_TOKEN"]\n'),
        ),
        patch("tend.checks._list_org_secrets", return_value=(None, True)),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"BOT_TOKEN"})
    assert result.passed is True
    assert "admin:org" in result.message


def test_repo_secret_allowlist_with_extra_allowed() -> None:
    """Additional allowed secret (e.g. CODECOV_TOKEN) — passes."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed(
                '["BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN","CODECOV_TOKEN"]\n'
            ),
        ),
        patch("tend.checks._list_org_secrets", return_value=(set(), False)),
    ):
        result = check_repo_secret_allowlist(
            "owner/repo", {"BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CODECOV_TOKEN"}
        )
    assert result.passed is True


def test_repo_secret_allowlist_empty_repo() -> None:
    """No secrets at all — passes."""
    with (
        patch("tend.checks._gh", return_value=_make_completed("[]\n")),
        patch("tend.checks._list_org_secrets", return_value=(set(), False)),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"BOT_TOKEN"})
    assert result.passed is True


def test_repo_secret_allowlist_api_error() -> None:
    with patch(
        "tend.checks._gh",
        return_value=_make_completed(returncode=1, stderr="HTTP 403"),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"BOT_TOKEN"})
    assert result.passed is None


def test_repo_secret_allowlist_no_gh() -> None:
    with patch("tend.checks._gh", return_value=None):
        result = check_repo_secret_allowlist("owner/repo", {"BOT_TOKEN"})
    assert result.passed is None


def test_repo_secret_allowlist_bad_json() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("not json")):
        result = check_repo_secret_allowlist("owner/repo", {"BOT_TOKEN"})
    assert result.passed is None


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------


def test_run_all_checks_no_gh() -> None:
    with patch("shutil.which", return_value=None):
        results = run_all_checks(Config("bot", "write", "main", [], "T1", "T2", [], {}))
    assert len(results) == 1
    assert results[0].passed is None
    assert "gh CLI" in results[0].message


def test_run_all_checks_no_repo() -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks.detect_repo", return_value=None),
    ):
        results = run_all_checks(Config("bot", "write", "main", [], "T1", "T2", [], {}))
    assert len(results) == 1
    assert "detect" in results[0].message


_ALL_PASS_RULESET = _make_ruleset(["~ALL"])


def _fake_gh_all_pass(*args, **kwargs) -> subprocess.CompletedProcess[str]:
    """Simulate a gh CLI where all checks pass for owner/repo."""
    cmd = " ".join(args)
    if args[1] == "repos/owner/repo" and "--jq" in args and ".default_branch" in args:
        return _make_completed("main\n")
    if "rulesets" in cmd:
        return _make_completed(_ALL_PASS_RULESET)
    if "branches" in cmd:
        return _make_completed("true\n")
    if "collaborators" in cmd:
        return _make_completed("write\n")
    if "secrets" in cmd:
        return _make_completed('["T1","T2"]\n')
    return _make_completed(returncode=1)


def test_run_all_checks_with_explicit_repo() -> None:
    """Explicit --repo skips auto-detection."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(
            Config("bot", "write", "main", [], "T1", "T2", [], {}), repo="owner/repo"
        )
    assert all(r.passed is True for r in results)


def test_run_all_checks_allowlist_includes_bot_secrets() -> None:
    """Allowlist automatically includes bot_token and claude_token secrets."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(
            Config("bot", "write", "main", [], "T1", "T2", [], {}), repo="owner/repo"
        )
    allowlist_check = [r for r in results if r.name == "repo-secret-allowlist"]
    assert len(allowlist_check) == 1
    assert allowlist_check[0].passed is True


def test_run_all_checks_allowlist_catches_unexpected() -> None:
    """Unexpected repo-level secret is flagged."""

    def fake_gh_with_extra_secret(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        cmd = " ".join(args)
        if (
            args[1] == "repos/owner/repo"
            and "--jq" in args
            and ".default_branch" in args
        ):
            return _make_completed("main\n")
        if "rulesets" in cmd:
            return _make_completed(_ALL_PASS_RULESET)
        if "branches" in cmd:
            return _make_completed("true\n")
        if "collaborators" in cmd:
            return _make_completed("write\n")
        if "secrets" in cmd:
            return _make_completed('["T1","T2","PYPI_TOKEN"]\n')
        return _make_completed(returncode=1)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=fake_gh_with_extra_secret),
    ):
        results = run_all_checks(
            Config("bot", "write", "main", [], "T1", "T2", [], {}), repo="owner/repo"
        )
    allowlist_check = [r for r in results if r.name == "repo-secret-allowlist"]
    assert len(allowlist_check) == 1
    assert allowlist_check[0].passed is False
    assert "PYPI_TOKEN" in allowlist_check[0].message


def test_run_all_checks_with_protected_branches() -> None:
    """Protected branches produce additional branch-protection checks."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(
            Config("bot", "write", "main", ["v1", "v2"], "T1", "T2", [], {}),
            repo="owner/repo",
        )
    # default + v1 + v2 + bot-permission + secrets + allowlist = 6
    assert len(results) == 6
    bp_results = [r for r in results if r.name.startswith("branch-protection:")]
    assert len(bp_results) == 3
    assert {r.name for r in bp_results} == {
        "branch-protection:main",
        "branch-protection:v1",
        "branch-protection:v2",
    }
    assert all(r.passed is True for r in results)


def test_run_all_checks_deduplicates_default_branch() -> None:
    """If protected_branches includes the default branch, it's not checked twice."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(
            Config("bot", "write", "main", ["main", "v1"], "T1", "T2", [], {}),
            repo="owner/repo",
        )
    # main (deduped) + v1 + bot-permission + secrets + allowlist = 5
    assert len(results) == 5
    bp_results = [r for r in results if r.name.startswith("branch-protection:")]
    assert len(bp_results) == 2
    assert {r.name for r in bp_results} == {
        "branch-protection:main",
        "branch-protection:v1",
    }


# ---------------------------------------------------------------------------
# Fork mode checks
# ---------------------------------------------------------------------------


def test_bot_write_permission_fails_in_fork_mode() -> None:
    """In fork mode, write permission is too much — should fail."""
    with patch("tend.checks._gh", return_value=_make_completed("write\n")):
        result = check_bot_permission("owner/repo", "my-bot", mode="fork")
    assert result.passed is False
    assert "triage" in result.message


def test_bot_triage_permission_passes_in_fork_mode() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("triage\n")):
        result = check_bot_permission("owner/repo", "my-bot", mode="fork")
    assert result.passed is True


def test_fork_mode_skips_branch_protection() -> None:
    """run_all_checks in fork mode skips branch protection check."""

    def fake_gh(*args, **kwargs):
        cmd = " ".join(args)
        if args[1].startswith("repos/owner/repo") and ".default_branch" in " ".join(
            args
        ):
            return _make_completed("main\n")
        if "collaborators" in cmd:
            return _make_completed("triage\n")
        if "secrets" in cmd:
            return _make_completed('["T1","T2"]\n')
        return _make_completed(returncode=1)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=fake_gh),
    ):
        results = run_all_checks(
            Config("bot", "fork", "main", [], "T1", "T2", [], {}),
            repo="owner/repo",
        )
    assert len(results) == 4
    bp = results[0]
    assert bp.name == "branch-protection"
    assert bp.passed is True
    assert "Fork mode" in bp.message


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


def test_cli_check_failure_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    results = [
        CheckResult("branch-protection", False, "NOT protected"),
    ]
    with patch("tend.cli.run_all_checks", return_value=results):
        result = CliRunner().invoke(main, ["check"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_cli_check_skips_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_init_prints_check_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0
    assert "tend check" in result.output
