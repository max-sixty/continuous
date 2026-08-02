"""Tests for security checks module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tend.checks import (
    check_environment,
    fix_environment,
    ROLE_ID_ADMIN,
    ROLE_ID_MAINTAIN,
    ROLE_ID_WRITE,
    CheckResult,
    _has_restrict_updates_ruleset,
    _restrict_updates_ruleset,
    check_bot_permission,
    check_branch_protection,
    check_repo_secret_allowlist,
    check_secrets,
    detect_canonical_owner,
    detect_repo,
    run_all_checks,
)
from tend.cli import main
from tend.config import Config


def _config(
    *,
    bot_name: str = "bot",
    default_branch: str = "main",
    protected_branches: list[str] | None = None,
    bot_token_secret: str = "T1",
    claude_token_secret: str = "T2",
    harness: str = "claude",
    model: str = "opus",
) -> Config:
    """Build a Config for tests without hand-listing every positional arg."""
    return Config(
        bot_name=bot_name,
        default_branch=default_branch,
        protected_branches=protected_branches or [],
        bot_token_secret=bot_token_secret,
        claude_token_secret=claude_token_secret,
        anthropic_api_key_secret="ANTHROPIC_API_KEY",
        openai_key_secret="OPENAI_API_KEY",
        harness=harness,
        model=model,
        effort="",
        setup=[],
        workflows={},
    )


def _make_completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _write_config(tmp_path: Path, content: str = "bot_name: test-bot") -> Path:
    cfg = tmp_path / ".config" / "tend.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(content)
    return cfg


def _make_branch_rules(
    *rule_types: str,
    ruleset_id: int | None = 1,
    source_type: str = "Repository",
    source: str = "owner/repo",
) -> str:
    """Build a JSON array of branch rules (as returned by /rules/branches/{branch})."""
    rule: dict[str, object]
    rules = []
    for t in rule_types:
        rule = {"type": t, "ruleset_source_type": source_type, "ruleset_source": source}
        if ruleset_id is not None:
            rule["ruleset_id"] = ruleset_id
        rules.append(rule)
    return json.dumps(rules)


def _role_actor(actor_id: int) -> dict[str, object]:
    """A `bypass_actors` entry granting a base repository role."""
    return {
        "actor_id": actor_id,
        "actor_type": "RepositoryRole",
        "bypass_mode": "exempt",
    }


def _gh_ruleset(
    rules: str,
    bypass_actors: list[dict[str, object]] | None,
    user_id: int | None = None,
    ruleset_json: str | None = None,
) -> object:
    """Build a `_gh` fake serving the calls `_has_restrict_updates_ruleset` makes:
    `/rules/branches/<branch>` returns `rules`; `/rulesets/<id>` returns a ruleset
    with `bypass_actors` (or `ruleset_json` verbatim if given, or returncode=1 if
    both are None); `users/<login>` returns `user_id` (or returncode=1 if None)."""

    def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str] | None:
        if "/rules/branches/" in args[1]:
            return _make_completed(rules)
        if args[1].startswith("users/"):
            if user_id is None:
                return _make_completed(returncode=1)
            return _make_completed(f"{user_id}\n")
        if ruleset_json is not None:
            return _make_completed(ruleset_json)
        if bypass_actors is None:
            return _make_completed(returncode=1)
        return _make_completed(json.dumps({"bypass_actors": bypass_actors}))

    return fake


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
# detect_canonical_owner
# ---------------------------------------------------------------------------


def _gh_for(repo: str, api_body: dict | None) -> object:
    """Build a `_gh` fake: `gh repo view` returns `repo`; `gh api repos/<repo>`
    returns `api_body` as JSON (or returncode=1 if None)."""

    def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str] | None:
        if args[0] == "repo" and args[1] == "view":
            return _make_completed(f"{repo}\n")
        if args[0] == "api" and args[1].startswith("repos/"):
            if api_body is None:
                return _make_completed(returncode=1)
            return _make_completed(json.dumps(api_body) + "\n")
        return _make_completed(returncode=1)

    return fake


def test_detect_canonical_owner_non_fork() -> None:
    """Non-fork repo: API returns fork=false; use .owner.login."""
    body = {"fork": False, "owner": {"login": "PRQL"}, "source": None}
    with patch("tend.checks._gh", side_effect=_gh_for("PRQL/prql", body)):
        assert detect_canonical_owner() == "PRQL"


def test_detect_canonical_owner_walks_to_source_for_fork() -> None:
    """Fork-of-canonical (cloned-fork-only setup): use .source.owner.login
    so the guard matches the canonical, not whoever is running `tend init`."""
    body = {
        "fork": True,
        "owner": {"login": "max-sixty"},
        "source": {"owner": {"login": "PRQL"}},
    }
    with patch("tend.checks._gh", side_effect=_gh_for("max-sixty/prql", body)):
        assert detect_canonical_owner() == "PRQL"


def test_detect_canonical_owner_chained_fork_uses_source_not_parent() -> None:
    """Chained forks (alice → bob → canonical): .source is the root, so
    one API call resolves correctly without walking parent links."""
    body = {
        "fork": True,
        "owner": {"login": "alice"},
        "source": {"owner": {"login": "canonical-org"}},
    }
    with patch("tend.checks._gh", side_effect=_gh_for("alice/repo", body)):
        assert detect_canonical_owner() == "canonical-org"


def test_detect_canonical_owner_no_gh() -> None:
    """When `gh` isn't installed, both calls return None — degrade to None
    so cli.init warns rather than shipping an empty/wrong owner string."""
    with patch("tend.checks._gh", return_value=None):
        assert detect_canonical_owner() is None


def test_detect_canonical_owner_api_failure_returns_none() -> None:
    """If `gh repo view` works but the API call fails (rate limit, auth,
    network), return None rather than the view's possibly-fork answer.
    Shipping the fork owner in the guard would silently no-op on canonical —
    worse than no guard at all."""
    with patch("tend.checks._gh", side_effect=_gh_for("max-sixty/prql", None)):
        assert detect_canonical_owner() is None


# ---------------------------------------------------------------------------
# check_branch_protection
# ---------------------------------------------------------------------------


def test_branch_protected() -> None:
    """Protected via a restrict-updates ruleset the bot can't bypass."""
    branch_rules = _make_branch_rules("update")
    ruleset = json.dumps({"bypass_actors": [_role_actor(ROLE_ID_ADMIN)]})

    def fake_gh(*args, **kwargs):
        url = args[1]
        if "rules/branches" in url:
            return _make_completed(branch_rules)
        if "/rulesets/" in url:
            return _make_completed(ruleset)
        return _make_completed("true\n")

    with patch("tend.checks._gh", side_effect=fake_gh):
        result = check_branch_protection("owner/repo", "main", "my-bot")
    assert result.passed is True
    assert "restrict-updates ruleset" in result.message


def test_branch_not_protected() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("false\n")):
        result = check_branch_protection("owner/repo", "main", "my-bot")
    assert result.passed is False
    assert "NOT protected" in result.message


def test_branch_protection_api_error() -> None:
    with patch(
        "tend.checks._gh",
        return_value=_make_completed(returncode=1, stderr="Not Found"),
    ):
        result = check_branch_protection("owner/repo", "main", "my-bot")
    assert result.passed is None
    assert "API error" in result.message


def test_branch_protection_no_gh() -> None:
    with patch("tend.checks._gh", return_value=None):
        result = check_branch_protection("owner/repo", "main", "my-bot")
    assert result.passed is None


def test_branch_protected_ruleset_inconclusive_skips() -> None:
    """Branch is protected, no reviews, ruleset check inconclusive → SKIP not FAIL."""
    protection_data = json.dumps(
        {"required_pull_request_reviews": {"required_approving_review_count": 0}}
    )

    def fake_gh(*args, **kwargs):
        url = args[1]
        if url == "repos/owner/repo/branches/main" and ".protected" in args:
            return _make_completed("true\n")
        if "rules/branches" in url:
            return _make_completed(returncode=1, stderr="HTTP 403")
        if "branches/main/protection" in url:
            return _make_completed(protection_data)
        return _make_completed(returncode=1)

    with patch("tend.checks._gh", side_effect=fake_gh):
        result = check_branch_protection("owner/repo", "main", "my-bot")
    assert result.passed is None
    assert "could not verify that the bot cannot bypass" in result.message


def test_branch_protection_result_name_includes_branch() -> None:
    """Each branch gets a distinct check name for identification."""
    with patch("tend.checks._gh", return_value=_make_completed("false\n")):
        main_result = check_branch_protection("owner/repo", "main", "my-bot")
        v1_result = check_branch_protection("owner/repo", "v1", "my-bot")
    assert main_result.name == "branch-protection:main"
    assert v1_result.name == "branch-protection:v1"


# ---------------------------------------------------------------------------
# _has_restrict_updates_ruleset
# ---------------------------------------------------------------------------


def test_no_rules_for_branch() -> None:
    """No rules at all for this branch → False."""
    with patch("tend.checks._gh", return_value=_make_completed("[]\n")):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is False


def test_update_rule_present() -> None:
    """Update rule whose ruleset only admins bypass → True."""
    fake = _gh_ruleset(_make_branch_rules("update"), [_role_actor(ROLE_ID_ADMIN)])
    with patch("tend.checks._gh", side_effect=fake) as gh:
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is True
    assert gh.call_args.args[-1] == "repos/owner/repo/rulesets/1"


def test_org_ruleset_read_via_repo_endpoint() -> None:
    """The repo-scoped endpoint serves org-sourced rulesets too."""
    fake = _gh_ruleset(
        _make_branch_rules("update", source_type="Organization", source="owner"),
        [_role_actor(ROLE_ID_ADMIN)],
    )
    with patch("tend.checks._gh", side_effect=fake) as gh:
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is True
    assert gh.call_args.args[-1] == "repos/owner/repo/rulesets/1"


def test_ruleset_bypass_list_not_visible() -> None:
    """GitHub omits `bypass_actors` below ruleset-admin → unverifiable, not empty.

    Reading the missing key as an empty list would report "nobody bypasses" —
    a false pass for exactly the caller who can't see the danger.
    """
    fake = _gh_ruleset(
        _make_branch_rules("update"),
        None,
        ruleset_json=json.dumps({"current_user_can_bypass": "never"}),
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is None


def test_only_non_update_rules() -> None:
    """Branch has rules but none are update → False."""
    data = _make_branch_rules("deletion", "required_linear_history")
    with patch("tend.checks._gh", return_value=_make_completed(data)):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is False


def test_update_rule_among_others() -> None:
    """Update rule mixed with other rules → True."""
    fake = _gh_ruleset(
        _make_branch_rules("deletion", "update", "required_signatures"),
        [_role_actor(ROLE_ID_ADMIN)],
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is True


def test_update_rule_bypassed_by_write() -> None:
    """A write-role bypass defeats the update rule — the bot holds write.

    This is the hole the check missed: the rule is present and the branch looks
    protected, but the bot can merge anyway.
    """
    fake = _gh_ruleset(_make_branch_rules("update"), [_role_actor(ROLE_ID_WRITE)])
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is False


def test_update_rule_maintain_bypass_ok() -> None:
    """Maintain outranks the bot's write, so a maintain bypass still protects."""
    fake = _gh_ruleset(
        _make_branch_rules("update"),
        [_role_actor(ROLE_ID_ADMIN), _role_actor(ROLE_ID_MAINTAIN)],
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is True


def test_update_rule_org_admin_bypass_ok() -> None:
    """OrganizationAdmin isn't a repository role but still outranks the bot."""
    fake = _gh_ruleset(
        _make_branch_rules("update"),
        [{"actor_type": "OrganizationAdmin", "actor_id": None}],
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is True


def test_update_rule_bot_user_bypass() -> None:
    """A user exemption naming the bot is an explicit grant of the merge.

    Without resolving the bot's login to its id this reads as unverifiable, and
    an unverifiable check exits 0 — so the misconfiguration would pass.
    """
    fake = _gh_ruleset(
        _make_branch_rules("update"),
        [{"actor_type": "User", "actor_id": 999}],
        user_id=999,
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is False


def test_update_rule_other_user_bypass_ok() -> None:
    """A user exemption naming someone else doesn't let the bot through."""
    fake = _gh_ruleset(
        _make_branch_rules("update"),
        [{"actor_type": "User", "actor_id": 12345}],
        user_id=999,
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is True


def test_update_rule_user_bypass_unresolvable_login() -> None:
    """If the bot's login won't resolve, a user exemption stays unverifiable."""
    fake = _gh_ruleset(
        _make_branch_rules("update"),
        [{"actor_type": "User", "actor_id": 999}],
        user_id=None,
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is None


def test_update_rule_team_bypass_unresolved() -> None:
    """A team bypass could contain the bot; membership isn't visible → None."""
    fake = _gh_ruleset(
        _make_branch_rules("update"),
        [{"actor_type": "Team", "actor_id": 42}],
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is None


def test_update_rule_write_bypass_beats_unresolved() -> None:
    """A definite write bypass outweighs an unresolvable actor in the same list."""
    fake = _gh_ruleset(
        _make_branch_rules("update"),
        [{"actor_type": "Team", "actor_id": 42}, _role_actor(ROLE_ID_WRITE)],
    )
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is False


def test_update_rule_no_bypass_actors() -> None:
    """An empty bypass list means nobody bypasses → protected."""
    fake = _gh_ruleset(_make_branch_rules("update"), [])
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is True


def test_update_rule_ruleset_unreadable() -> None:
    """Update rule present but its ruleset can't be read → None, not a pass."""
    fake = _gh_ruleset(_make_branch_rules("update"), None)
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is None


def test_update_rule_without_ruleset_id() -> None:
    """An update rule we can't trace to a ruleset is unverified, not absent."""
    fake = _gh_ruleset(_make_branch_rules("update", ruleset_id=None), None)
    with patch("tend.checks._gh", side_effect=fake):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is None


def test_branch_rules_api_error() -> None:
    """API error → None (inconclusive)."""
    with patch(
        "tend.checks._gh",
        return_value=_make_completed(returncode=1, stderr="Not Found"),
    ):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is None


def test_branch_rules_no_gh() -> None:
    """gh CLI not found → None (can't check either endpoint)."""
    with patch("tend.checks._gh", return_value=None):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is None


def test_branch_rules_non_list_response() -> None:
    """API returns a JSON object instead of an array → None."""
    with patch(
        "tend.checks._gh",
        return_value=_make_completed('{"message": "Not Found"}'),
    ):
        assert _has_restrict_updates_ruleset("owner/repo", "main", "my-bot") is None


# ---------------------------------------------------------------------------
# _restrict_updates_ruleset
# ---------------------------------------------------------------------------


def test_ruleset_default_branch_only() -> None:
    """No extra branches — ruleset targets only ~DEFAULT_BRANCH."""
    body = json.loads(_restrict_updates_ruleset([]))
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]


def test_ruleset_with_extra_branches() -> None:
    """Extra branches are added as refs/heads/<name> patterns."""
    body = json.loads(_restrict_updates_ruleset(["release", "staging"]))
    assert body["conditions"]["ref_name"]["include"] == [
        "~DEFAULT_BRANCH",
        "refs/heads/release",
        "refs/heads/staging",
    ]


# ---------------------------------------------------------------------------
# check_bot_permission
# ---------------------------------------------------------------------------


def _permission_response(
    role_name: str, *, admin: bool = False, maintain: bool = False
) -> str:
    """The /collaborators/{user}/permission response, trimmed to what's read."""
    return json.dumps(
        {
            "permission": "admin" if admin else "write",
            "role_name": role_name,
            "user": {
                "permissions": {"admin": admin, "maintain": maintain, "push": True}
            },
        }
    )


def test_bot_write_permission() -> None:
    resp = _permission_response("write")
    with patch("tend.checks._gh", return_value=_make_completed(resp)):
        result = check_bot_permission("owner/repo", "my-bot")
    assert result.passed is True
    assert "write" in result.message


def test_bot_admin_permission() -> None:
    resp = _permission_response("admin", admin=True, maintain=True)
    with patch("tend.checks._gh", return_value=_make_completed(resp)):
        result = check_bot_permission("owner/repo", "my-bot")
    assert result.passed is False
    assert "admin" in result.message
    assert "bypass" in result.message


def test_bot_maintain_permission() -> None:
    """Maintain bypasses the merge restriction, so the bot must not hold it.

    The legacy `.permission` field reports a maintain collaborator as "write",
    which is why the check reads the `permissions` booleans instead.
    """
    resp = _permission_response("maintain", maintain=True)
    with patch("tend.checks._gh", return_value=_make_completed(resp)):
        result = check_bot_permission("owner/repo", "my-bot")
    assert result.passed is False
    assert "maintain" in result.message
    assert "bypass" in result.message


def test_bot_custom_role_with_maintain_fails() -> None:
    """A custom role is judged by its capabilities, not its name.

    Its `role_name` matches no base role, so only the `permissions` booleans
    reveal that it can bypass.
    """
    resp = _permission_response("release-manager", maintain=True)
    with patch("tend.checks._gh", return_value=_make_completed(resp)):
        result = check_bot_permission("owner/repo", "my-bot")
    assert result.passed is False
    assert "release-manager" in result.message


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
        return_value=_make_completed('["TEND_BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"]\n'),
    ):
        result = check_secrets(
            "owner/repo", ["TEND_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
        )
    assert result.passed is True


def test_secrets_missing() -> None:
    with patch("tend.checks._gh", return_value=_make_completed('["TEND_BOT_TOKEN"]\n')):
        result = check_secrets(
            "owner/repo", ["TEND_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
        )
    assert result.passed is False
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.message
    assert "admin:org" not in result.message


def test_secrets_missing_with_org_403_hint() -> None:
    """When org secrets return 403 and secrets are missing, include the hint."""
    with (
        patch("tend.checks._gh", return_value=_make_completed('["TEND_BOT_TOKEN"]\n')),
        patch("tend.checks._list_org_secrets", return_value=(None, True)),
    ):
        result = check_secrets(
            "owner/repo", ["TEND_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
        )
    assert result.passed is False
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.message
    assert "admin:org" in result.message
    assert "gh auth refresh" in result.message


def test_secrets_api_error() -> None:
    with patch(
        "tend.checks._gh", return_value=_make_completed(returncode=1, stderr="HTTP 403")
    ):
        result = check_secrets("owner/repo", ["TEND_BOT_TOKEN"])
    assert result.passed is None


def test_secrets_bad_json() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("not json")):
        result = check_secrets("owner/repo", ["TEND_BOT_TOKEN"])
    assert result.passed is None


# ---------------------------------------------------------------------------
# check_repo_secret_allowlist
# ---------------------------------------------------------------------------


def test_repo_secret_allowlist_pass() -> None:
    """Only allowed secrets at repo level, no org secrets — passes."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed(
                '["TEND_BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN"]\n'
            ),
        ),
        patch("tend.checks._list_org_secrets", return_value=(set(), False)),
    ):
        result = check_repo_secret_allowlist(
            "owner/repo", {"TEND_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
        )
    assert result.passed is True
    assert "in allowlist" in result.message


def test_repo_secret_allowlist_unexpected_repo() -> None:
    """Unexpected secret at repo level — fails with repo-level annotation."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed(
                '["TEND_BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN","PYPI_TOKEN"]\n'
            ),
        ),
        patch("tend.checks._list_org_secrets", return_value=(set(), False)),
    ):
        result = check_repo_secret_allowlist(
            "owner/repo", {"TEND_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
        )
    assert result.passed is False
    assert "PYPI_TOKEN" in result.message
    assert "repo-level" in result.message


def test_repo_secret_allowlist_unexpected_org() -> None:
    """Unexpected secret at org level — fails with org-level annotation."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed('["TEND_BOT_TOKEN"]\n'),
        ),
        patch(
            "tend.checks._list_org_secrets",
            return_value=({"TEND_BOT_TOKEN", "NPM_TOKEN"}, False),
        ),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"TEND_BOT_TOKEN"})
    assert result.passed is False
    assert "NPM_TOKEN" in result.message
    assert "org-level" in result.message


def test_repo_secret_allowlist_unexpected_both() -> None:
    """Unexpected secrets at both levels — message includes both annotations."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed('["TEND_BOT_TOKEN","PYPI_TOKEN"]\n'),
        ),
        patch(
            "tend.checks._list_org_secrets",
            return_value=({"NPM_TOKEN"}, False),
        ),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"TEND_BOT_TOKEN"})
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
            return_value=_make_completed('["TEND_BOT_TOKEN"]\n'),
        ),
        patch(
            "tend.checks._list_org_secrets",
            return_value=({"CODECOV_TOKEN"}, False),
        ),
    ):
        result = check_repo_secret_allowlist(
            "owner/repo", {"TEND_BOT_TOKEN", "CODECOV_TOKEN"}
        )
    assert result.passed is True


def test_repo_secret_allowlist_org_forbidden() -> None:
    """Org secrets return 403 — passes but notes the gap."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed('["TEND_BOT_TOKEN"]\n'),
        ),
        patch("tend.checks._list_org_secrets", return_value=(None, True)),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"TEND_BOT_TOKEN"})
    assert result.passed is True
    assert "admin:org" in result.message


def test_repo_secret_allowlist_with_extra_allowed() -> None:
    """Additional allowed secret (e.g. CODECOV_TOKEN) — passes."""
    with (
        patch(
            "tend.checks._gh",
            return_value=_make_completed(
                '["TEND_BOT_TOKEN","CLAUDE_CODE_OAUTH_TOKEN","CODECOV_TOKEN"]\n'
            ),
        ),
        patch("tend.checks._list_org_secrets", return_value=(set(), False)),
    ):
        result = check_repo_secret_allowlist(
            "owner/repo", {"TEND_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CODECOV_TOKEN"}
        )
    assert result.passed is True


def test_repo_secret_allowlist_empty_repo() -> None:
    """No secrets at all — passes."""
    with (
        patch("tend.checks._gh", return_value=_make_completed("[]\n")),
        patch("tend.checks._list_org_secrets", return_value=(set(), False)),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"TEND_BOT_TOKEN"})
    assert result.passed is True


def test_repo_secret_allowlist_api_error() -> None:
    with patch(
        "tend.checks._gh",
        return_value=_make_completed(returncode=1, stderr="HTTP 403"),
    ):
        result = check_repo_secret_allowlist("owner/repo", {"TEND_BOT_TOKEN"})
    assert result.passed is None


def test_repo_secret_allowlist_no_gh() -> None:
    with patch("tend.checks._gh", return_value=None):
        result = check_repo_secret_allowlist("owner/repo", {"TEND_BOT_TOKEN"})
    assert result.passed is None


def test_repo_secret_allowlist_bad_json() -> None:
    with patch("tend.checks._gh", return_value=_make_completed("not json")):
        result = check_repo_secret_allowlist("owner/repo", {"TEND_BOT_TOKEN"})
    assert result.passed is None


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------


def test_run_all_checks_no_gh() -> None:
    with patch("shutil.which", return_value=None):
        results = run_all_checks(_config())
    assert len(results) == 1
    assert results[0].passed is None
    assert "gh CLI" in results[0].message


def test_run_all_checks_no_repo() -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks.detect_repo", return_value=None),
    ):
        results = run_all_checks(_config())
    assert len(results) == 1
    assert "detect" in results[0].message


_BRANCH_HAS_UPDATE_RULE = _make_branch_rules("update")


def _fake_gh_all_pass(*args, **kwargs) -> subprocess.CompletedProcess[str]:
    """Simulate a gh CLI where all checks pass for owner/repo."""
    url = args[1]
    if url == "repos/owner/repo" and "--jq" in args and ".default_branch" in args:
        return _make_completed("main\n")
    if "rules/branches" in url:
        return _make_completed(_BRANCH_HAS_UPDATE_RULE)
    if "/rulesets/" in url:
        return _make_completed(
            json.dumps({"bypass_actors": [_role_actor(ROLE_ID_ADMIN)]})
        )
    if "branches" in url:
        return _make_completed("true\n")
    if "collaborators" in url:
        return _make_completed(_permission_response("write"))
    if url.endswith("deployment-branch-policies"):
        return _make_completed('["main"]\n')
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
    # Endpoint-specific on purpose: the operational secrets live in the
    # environment and must be absent from repo level, so a fake that answered
    # both the same way would let a check read the wrong one and still pass.
    if "environments/tend/secrets" in url:
        return _make_completed('["T1","T2"]\n')
    if "secrets" in url:
        return _make_completed("[]\n")
    return _make_completed(returncode=1)


def test_run_all_checks_with_explicit_repo() -> None:
    """Explicit --repo skips auto-detection."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
    assert all(r.passed is True for r in results)


def test_run_all_checks_flags_operational_secrets_left_at_repo_level() -> None:
    """A repo-level copy of an operational secret defeats the environment.

    Any workflow the bot can push reads a repo-level secret without naming the
    environment, so a leftover copy gives back exactly what the deployment
    branch policy denies — and it is invisible otherwise, because everything
    keeps working. The allowlist is what surfaces it: the operational names are
    deliberately not in the allowed set.
    """

    def gh_with_repo_level_copy(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        url = args[1]
        if "environments/tend/secrets" not in url and url.endswith("actions/secrets"):
            return _make_completed('["T1"]\n')
        return _fake_gh_all_pass(*args, **kwargs)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=gh_with_repo_level_copy),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
    allowlist = [r for r in results if r.name == "repo-secret-allowlist"]
    assert len(allowlist) == 1
    assert allowlist[0].passed is False, (
        "a repo-level copy of the bot token must be flagged — it is readable "
        "from any branch the bot can push"
    )
    assert "T1" in allowlist[0].message


def test_run_all_checks_allowlist_catches_unexpected() -> None:
    """Unexpected repo-level secret is flagged."""

    def fake_gh_with_extra_secret(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        url = args[1]
        if url == "repos/owner/repo" and "--jq" in args and ".default_branch" in args:
            return _make_completed("main\n")
        if "rules/branches" in url:
            return _make_completed(_BRANCH_HAS_UPDATE_RULE)
        if "/rulesets/" in url:
            return _make_completed(
                json.dumps({"bypass_actors": [_role_actor(ROLE_ID_ADMIN)]})
            )
        if "branches" in url:
            return _make_completed("true\n")
        if "collaborators" in url:
            return _make_completed(_permission_response("write"))
        if "secrets" in url:
            return _make_completed('["T1","T2","PYPI_TOKEN"]\n')
        return _make_completed(returncode=1)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=fake_gh_with_extra_secret),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
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
            _config(protected_branches=["v1", "v2"]),
            repo="owner/repo",
        )
    # default + v1 + v2 + bot-permission + secrets + claude-auth + allowlist = 7
    assert len(results) == 8
    bp_results = [r for r in results if r.name.startswith("branch-protection:")]
    assert len(bp_results) == 3
    assert {r.name for r in bp_results} == {
        "branch-protection:main",
        "branch-protection:v1",
        "branch-protection:v2",
    }
    assert all(r.passed is True for r in results)


def test_codex_engine_passes_with_openai_key() -> None:
    """Engine=codex with OPENAI_API_KEY set passes the codex-auth check."""

    def fake_gh(*args, **kwargs):
        url = args[1]
        if url == "repos/owner/repo" and "--jq" in args and ".default_branch" in args:
            return _make_completed("main\n")
        if "rules/branches" in url:
            return _make_completed(_BRANCH_HAS_UPDATE_RULE)
        if "branches" in url:
            return _make_completed("true\n")
        if "collaborators" in url:
            return _make_completed("write\n")
        if "secrets" in url:
            if "--json" in args:
                return _make_completed('[{"name":"T1"},{"name":"OPENAI_API_KEY"}]\n')
            return _make_completed('["T1","OPENAI_API_KEY"]\n')
        return _make_completed(returncode=1)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=fake_gh),
    ):
        results = run_all_checks(_config(harness="codex"), repo="owner/repo")
    codex_check = [r for r in results if r.name == "codex-auth"]
    assert len(codex_check) == 1
    assert codex_check[0].passed is True
    assert "OPENAI_API_KEY" in codex_check[0].message


def test_codex_engine_fails_when_no_auth() -> None:
    """Engine=codex with OPENAI_API_KEY unset is a hard failure."""

    def fake_gh(*args, **kwargs):
        url = args[1]
        if url == "repos/owner/repo" and "--jq" in args and ".default_branch" in args:
            return _make_completed("main\n")
        if "rules/branches" in url:
            return _make_completed(_BRANCH_HAS_UPDATE_RULE)
        if "branches" in url:
            return _make_completed("true\n")
        if "collaborators" in url:
            return _make_completed("write\n")
        if "secrets" in url:
            if "--json" in args:
                return _make_completed('[{"name":"T1"}]\n')
            return _make_completed('["T1"]\n')
        return _make_completed(returncode=1)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=fake_gh),
    ):
        results = run_all_checks(_config(harness="codex"), repo="owner/repo")
    codex_check = [r for r in results if r.name == "codex-auth"]
    assert codex_check[0].passed is False
    assert "OPENAI_API_KEY" in codex_check[0].message


def test_claude_engine_omits_codex_auth_check() -> None:
    """The codex-auth check only runs when harness=codex."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
    assert not any(r.name == "codex-auth" for r in results)


def test_claude_engine_passes_with_oauth_token() -> None:
    """Engine=claude with the OAuth token secret set passes claude-auth."""
    # _fake_gh_all_pass returns ["T1","T2"] — T2 is claude_token_secret.
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
    claude_check = [r for r in results if r.name == "claude-auth"]
    assert len(claude_check) == 1
    assert claude_check[0].passed is True
    assert "T2" in claude_check[0].message


def test_claude_engine_passes_with_api_key() -> None:
    """Engine=claude with only ANTHROPIC_API_KEY set passes claude-auth."""

    def fake_gh(*args, **kwargs):
        url = args[1]
        if url == "repos/owner/repo" and "--jq" in args and ".default_branch" in args:
            return _make_completed("main\n")
        if "rules/branches" in url:
            return _make_completed(_BRANCH_HAS_UPDATE_RULE)
        if "branches" in url:
            return _make_completed("true\n")
        if "collaborators" in url:
            return _make_completed("write\n")
        if "secrets" in url:
            if "--json" in args:
                return _make_completed('[{"name":"T1"},{"name":"ANTHROPIC_API_KEY"}]\n')
            return _make_completed('["T1","ANTHROPIC_API_KEY"]\n')
        return _make_completed(returncode=1)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=fake_gh),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
    claude_check = [r for r in results if r.name == "claude-auth"]
    assert claude_check[0].passed is True
    assert "ANTHROPIC_API_KEY" in claude_check[0].message


def test_claude_engine_fails_when_no_auth() -> None:
    """Engine=claude with neither secret set is a hard failure."""

    def fake_gh(*args, **kwargs):
        url = args[1]
        if url == "repos/owner/repo" and "--jq" in args and ".default_branch" in args:
            return _make_completed("main\n")
        if "rules/branches" in url:
            return _make_completed(_BRANCH_HAS_UPDATE_RULE)
        if "branches" in url:
            return _make_completed("true\n")
        if "collaborators" in url:
            return _make_completed("write\n")
        if "secrets" in url:
            if "--json" in args:
                return _make_completed('[{"name":"T1"}]\n')
            return _make_completed('["T1"]\n')
        return _make_completed(returncode=1)

    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=fake_gh),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
    claude_check = [r for r in results if r.name == "claude-auth"]
    assert claude_check[0].passed is False
    assert "T2" in claude_check[0].message
    assert "ANTHROPIC_API_KEY" in claude_check[0].message


def test_run_all_checks_deduplicates_default_branch() -> None:
    """If protected_branches includes the default branch, it's not checked twice."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(
            _config(protected_branches=["main", "v1"]),
            repo="owner/repo",
        )
    # main (deduped) + v1 + bot-permission + secrets + claude-auth + allowlist = 6
    assert len(results) == 7
    bp_results = [r for r in results if r.name.startswith("branch-protection:")]
    assert len(bp_results) == 2
    assert {r.name for r in bp_results} == {
        "branch-protection:main",
        "branch-protection:v1",
    }


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


# ---------------------------------------------------------------------------
# Environment gate
#
# The environment is the mechanism, not a nicety: a job naming it runs only
# from a ref in its deployment branch policy, which is what refuses a workflow
# pushed to a feature branch before its first step. Each way the policy can be
# too generous is a way the secrets come back.
# ---------------------------------------------------------------------------


def _env_gh(env_body: str | None, policies: str = '["main"]\n"'):
    def fake(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        url = args[1]
        if url.endswith("deployment-branch-policies"):
            return _make_completed(policies.rstrip('"'))
        if url.endswith("environments/tend"):
            if env_body is None:
                return _make_completed(returncode=1)
            return _make_completed(env_body)
        return _make_completed(returncode=1)

    return fake


def test_environment_missing_fails() -> None:
    with patch("tend.checks._gh", side_effect=_env_gh(None)):
        result = check_environment("owner/repo", ["main"])
    assert result.passed is False
    assert "not found" in result.message


def test_environment_without_branch_policy_fails() -> None:
    """No policy means every ref reaches the secrets, including a bot branch."""
    with patch(
        "tend.checks._gh",
        side_effect=_env_gh(json.dumps({"deployment_branch_policy": None})),
    ):
        result = check_environment("owner/repo", ["main"])
    assert result.passed is False
    assert "no deployment branch policy" in result.message


def test_environment_protected_branches_policy_fails() -> None:
    """`protected_branches` keys on whether a ruleset covers the branch, not on
    who may push it, so a branch the bot can push can still be admitted."""
    body = json.dumps(
        {
            "deployment_branch_policy": {
                "protected_branches": True,
                "custom_branch_policies": False,
            }
        }
    )
    with patch("tend.checks._gh", side_effect=_env_gh(body)):
        result = check_environment("owner/repo", ["main"])
    assert result.passed is False
    assert "all protected branches" in result.message


def test_environment_extra_admitted_branch_fails() -> None:
    """A ref tend does not verify the bot is kept off must not be admitted."""
    body = json.dumps(
        {
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            }
        }
    )
    with patch(
        "tend.checks._gh", side_effect=_env_gh(body, policies='["main","staging"]\n"')
    ):
        result = check_environment("owner/repo", ["main"])
    assert result.passed is False
    assert "staging" in result.message


def test_environment_admitting_only_verified_refs_passes() -> None:
    body = json.dumps(
        {
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            }
        }
    )
    with patch(
        "tend.checks._gh", side_effect=_env_gh(body, policies='["main","release"]\n"')
    ):
        result = check_environment("owner/repo", ["main", "release"])
    assert result.passed is True


def test_fix_environment_reconciles_the_admitted_set() -> None:
    """The fix leaves exactly the admitted refs — a ref left over from an
    earlier config is a ref the bot may be able to push."""
    calls: list[tuple[str, ...]] = []

    def fake(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[-1].endswith("deployment-branch-policies"):
            return _make_completed(
                json.dumps({"branch_policies": [{"name": "stale", "id": 7}]})
            )
        return _make_completed("{}")

    with patch("tend.checks._gh", side_effect=fake):
        result = fix_environment("owner/repo", ["main", "release"])

    assert result.passed is True
    added = {
        arg.split("=", 1)[1]
        for call in calls
        for arg in call
        if arg.startswith("name=")
    }
    assert added == {"main", "release"}
    assert any(
        "DELETE" in a and a[-1].endswith("deployment-branch-policies/7") for a in calls
    )
