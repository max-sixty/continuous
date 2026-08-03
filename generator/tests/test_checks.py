"""Tests for security checks module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tend.checks import (
    ROLE_ID_ADMIN,
    ROLE_ID_MAINTAIN,
    ROLE_ID_WRITE,
    CheckResult,
    _has_restrict_updates_ruleset,
    _pattern_covers,
    _restrict_updates_ruleset,
    check_bot_permission,
    check_branch_protection,
    check_release_protection,
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
    url = next(a for a in args[1:] if not a.startswith("-"))
    if url == "repos/owner/repo" and "--jq" in args and ".default_branch" in args:
        return _make_completed("main\n")
    if url == "repos/owner/repo/environments":
        return _make_completed("")  # --paginate --jq: no environments, no lines
    if url == "graphql":
        return _make_completed(json.dumps({"data": {"repository": {"object": None}}}))
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
        return _make_completed('["T1","T2"]\n')
    return _make_completed(returncode=1)


def test_run_all_checks_with_explicit_repo() -> None:
    """Explicit --repo skips auto-detection."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
    assert all(r.passed is True for r in results)


def test_run_all_checks_allowlist_includes_bot_secrets() -> None:
    """Allowlist automatically includes bot_token and claude_token secrets."""
    with (
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("tend.checks._gh", side_effect=_fake_gh_all_pass),
    ):
        results = run_all_checks(_config(), repo="owner/repo")
    allowlist_check = [r for r in results if r.name == "repo-secret-allowlist"]
    assert len(allowlist_check) == 1
    assert allowlist_check[0].passed is True


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
    # default + v1 + v2 + bot-permission + secrets + claude-auth + allowlist
    # + release-protection = 8
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
    # main (deduped) + v1 + bot-permission + secrets + claude-auth + allowlist
    # + release-protection = 7
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
# check_release_protection
# ---------------------------------------------------------------------------

_RELEASE_WORKFLOW = """
name: release
on:
  push:
    tags: ["v*"]
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - run: echo publish
"""


def _tag_ruleset(
    ruleset_id: int = 7,
    *,
    include: list[str] | None = None,
    rules: tuple[str, ...] = ("creation", "update"),
    bypass: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": ruleset_id,
        "name": "Tag operations",
        "target": "tag",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": include or ["~ALL"], "exclude": []}},
        "rules": [{"type": r} for r in rules],
        "bypass_actors": bypass if bypass is not None else [_role_actor(ROLE_ID_ADMIN)],
    }


def _gh_release(
    *,
    environments: list[dict[str, object]] | None = None,
    env_secrets: dict[str, list[str]] | None = None,
    policies: dict[str, list[dict[str, str]]] | None = None,
    rulesets: list[dict[str, object]] | None = None,
    unreadable_ruleset_ids: set[int] | None = None,
    workflows: dict[str, str] | None = None,
    fail: str | None = None,
) -> object:
    """Build a `_gh` fake serving every call `check_release_protection` makes.

    `fail` names a URL fragment whose call should return returncode=1, so a
    test can exercise one unreadable endpoint at a time.
    """
    environments = environments or []
    env_secrets = env_secrets or {}
    policies = policies or {}
    rulesets = rulesets or []
    workflows = workflows if workflows is not None else {}

    def lines(items: list) -> subprocess.CompletedProcess[str]:
        """`gh api --paginate --jq` output: one JSON result per line."""
        return _make_completed("".join(json.dumps(i) + "\n" for i in items))

    def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str] | None:
        # The endpoint is the first positional after `api`; flags may precede it.
        url = next(a for a in args[1:] if not a.startswith("-"))
        if fail and fail in url:
            return _make_completed(returncode=1)
        if url == "graphql":
            entries = [
                {"name": n, "type": "blob", "object": {"text": t}}
                for n, t in workflows.items()
            ]
            return _make_completed(
                json.dumps({"data": {"repository": {"object": {"entries": entries}}}})
            )
        if url.endswith("/environments"):
            return lines(environments)
        if url.endswith("/secrets"):
            env = url.split("/environments/")[1].split("/")[0]
            return lines([{"name": n} for n in env_secrets.get(env, [])])
        if url.endswith("/deployment-branch-policies"):
            env = url.split("/environments/")[1].split("/")[0]
            return lines(policies.get(env, []))
        if "/rulesets/" in url:
            wanted = int(url.rsplit("/", 1)[1])
            if wanted in (unreadable_ruleset_ids or set()):
                return _make_completed(returncode=1)
            for rs in rulesets:
                if rs["id"] == wanted:
                    return _make_completed(json.dumps(rs))
            return _make_completed(returncode=1)
        if url.endswith("/rulesets"):
            return lines(rulesets)
        return _make_completed(returncode=1)

    return fake


def _release_check(fake: object, *, allowed: set[str] | None = None) -> CheckResult:
    with patch("tend.checks._gh", side_effect=fake):
        return check_release_protection(
            "owner/repo", _config(), "main", allowed if allowed is not None else {"T1"}
        )


def test_release_protection_no_publish_surface_passes() -> None:
    """A repo with no environments and no OIDC has nothing to gate — it must
    not be told it is misconfigured."""
    result = _release_check(_gh_release(workflows={"ci.yaml": "on: push\njobs: {}\n"}))
    assert result.passed is True
    assert "No environment or workflow" in result.message


def test_release_protection_open_environment_fails() -> None:
    """The reported gap: an environment with no deployment branch policy is
    reachable from any ref a write-scoped bot can push."""
    result = _release_check(
        _gh_release(
            environments=[
                {
                    "name": "pypi",
                    "deployment_branch_policy": None,
                    "protection_rules": [],
                }
            ],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is False
    assert "environment 'pypi' has no deployment branch policy" in result.message


def test_release_protection_environment_of_allowlisted_secrets_passes() -> None:
    """Moving tend's own secrets into an environment is hardening; the
    environment holds nothing the bot does not already have at repo level."""
    result = _release_check(
        _gh_release(
            environments=[
                {
                    "name": "tend",
                    "deployment_branch_policy": None,
                    "protection_rules": [],
                }
            ],
            env_secrets={"tend": ["T1"]},
            workflows={"ci.yaml": "on: push\njobs: {}\n"},
        )
    )
    assert result.passed is True


def test_release_protection_required_reviewers_pass() -> None:
    """Required reviewers gate every trigger, so they cover an open ref policy."""
    result = _release_check(
        _gh_release(
            environments=[
                {
                    "name": "pypi",
                    "deployment_branch_policy": None,
                    "protection_rules": [
                        {
                            "type": "required_reviewers",
                            "reviewers": [
                                {"type": "User", "reviewer": {"login": "alice"}}
                            ],
                        }
                    ],
                }
            ],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is True


def test_release_protection_bot_as_sole_reviewer_fails() -> None:
    """Naming the bot as the only required reviewer hands it its own approval."""
    result = _release_check(
        _gh_release(
            environments=[
                {
                    "name": "pypi",
                    "deployment_branch_policy": None,
                    "protection_rules": [
                        {
                            "type": "required_reviewers",
                            "reviewers": [
                                {"type": "User", "reviewer": {"login": "bot"}}
                            ],
                        }
                    ],
                }
            ],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is False


def _tag_pinned_env() -> list[dict[str, object]]:
    return [
        {
            "name": "pypi",
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
            "protection_rules": [{"type": "branch_policy"}],
        }
    ]


def test_release_protection_tag_policy_with_ruleset_passes() -> None:
    """The prescribed chain: environment pinned to tags, tag ruleset gating
    creation and update with bypass above write."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset()],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is True


def test_release_protection_tag_policy_without_ruleset_fails() -> None:
    """A tag-pinned environment with no tag ruleset is the escalation: the bot
    pushes a matching tag and the release workflow runs."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is False
    assert "tag pattern 'v*'" in result.message


def test_release_protection_tag_ruleset_without_update_rule_fails() -> None:
    """`creation` alone lets the bot re-point an admin-pushed tag."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset(rules=("creation",))],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is False


def test_release_protection_tag_ruleset_bypassable_by_bot_fails() -> None:
    """A write-role bypass actor defeats the ruleset — write is what the bot has."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset(bypass=[_role_actor(ROLE_ID_WRITE)])],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is False


def test_release_protection_narrower_tag_ruleset_does_not_cover() -> None:
    """A ruleset on `v*` does not gate a policy admitting every tag."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "*", "type": "tag"}]},
            rulesets=[_tag_ruleset(include=["refs/tags/v*"])],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is False


def test_release_protection_default_branch_policy_passes() -> None:
    """A continuous-deploy environment pinned to the merge-restricted default
    branch is gated; `check_branch_protection` verifies that branch itself."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "main", "type": "branch"}]},
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is True


def test_release_protection_wildcard_branch_policy_fails() -> None:
    """A branch glob admits branches the bot can create."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "release/*", "type": "branch"}]},
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is False
    assert "branch pattern 'release/*'" in result.message


def test_release_protection_protected_branches_policy_passes() -> None:
    """`protected_branches: true` rejects every tag and admits only branches
    carrying a classic protection rule."""
    result = _release_check(
        _gh_release(
            environments=[
                {
                    "name": "pypi",
                    "deployment_branch_policy": {
                        "protected_branches": True,
                        "custom_branch_policies": False,
                    },
                    "protection_rules": [{"type": "branch_policy"}],
                }
            ],
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is True


def test_release_protection_steerable_trigger_on_gated_environment_fails() -> None:
    """A tag ruleset does not stop the bot creating a release against a tag an
    admin already pushed, and the release body and assets are the bot's."""
    workflow = _RELEASE_WORKFLOW.replace(
        'on:\n  push:\n    tags: ["v*"]\n', "on:\n  release:\n    types: [published]\n"
    )
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset()],
            workflows={"release.yaml": workflow},
        )
    )
    assert result.passed is False
    assert "`release`" in result.message


def test_release_protection_input_free_dispatch_is_not_steerable() -> None:
    """A `workflow_dispatch` with no inputs only re-runs code the ref fixes, so
    against an admin-gated ref it republishes what an admin already published."""
    workflow = _RELEASE_WORKFLOW.replace(
        'on:\n  push:\n    tags: ["v*"]\n', "on:\n  workflow_dispatch:\n"
    )
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset()],
            workflows={"release.yaml": workflow},
        )
    )
    assert result.passed is True


def test_release_protection_dispatch_with_inputs_is_steerable() -> None:
    workflow = _RELEASE_WORKFLOW.replace(
        'on:\n  push:\n    tags: ["v*"]\n',
        "on:\n  workflow_dispatch:\n    inputs:\n      version:\n        type: string\n",
    )
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset()],
            workflows={"release.yaml": workflow},
        )
    )
    assert result.passed is False
    assert "`workflow_dispatch`" in result.message


def test_release_protection_oidc_outside_environment_fails() -> None:
    """No environment means no environment claim and no ref gate — a relying
    party that does not pin the ref accepts a token from any branch."""
    workflow = """
on:
  push:
    tags: ["v*"]
permissions:
  id-token: write
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: echo publish
"""
    result = _release_check(_gh_release(workflows={"release.yaml": workflow}))
    assert result.passed is False
    assert "release.yaml:publish" in result.message


def test_release_protection_job_permissions_replace_workflow_permissions() -> None:
    """A job-level `permissions:` block replaces the workflow-level one, so a
    job that omits id-token does not inherit it."""
    workflow = """
on: push
permissions:
  id-token: write
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - run: echo build
"""
    result = _release_check(_gh_release(workflows={"ci.yaml": workflow}))
    assert result.passed is True


def test_release_protection_reusable_workflow_inherits_caller_triggers() -> None:
    """A reusable workflow's `on:` says only that it is callable; what starts
    it is whatever starts its caller."""
    caller = """
on:
  release:
    types: [published]
jobs:
  call:
    uses: ./.github/workflows/publish.yaml
"""
    callee = """
on:
  workflow_call:
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - run: echo publish
"""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset()],
            workflows={"release.yaml": caller, "publish.yaml": callee},
        )
    )
    assert result.passed is False
    assert "`release`" in result.message


def test_release_protection_unreached_reusable_workflow_is_unverified() -> None:
    """A reusable workflow with no caller in this repo may be called from
    another — report that rather than calling the environment gated."""
    callee = """
on:
  workflow_call:
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - run: echo publish
"""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset()],
            workflows={"publish.yaml": callee},
        )
    )
    assert result.passed is None
    assert "workflow_call" in result.message


def test_release_protection_dynamic_environment_name_is_unverified() -> None:
    workflow = """
on:
  push:
    tags: ["v*"]
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: ${{ inputs.target }}
    steps:
      - run: echo publish
"""
    result = _release_check(_gh_release(workflows={"release.yaml": workflow}))
    assert result.passed is None
    assert "dynamically" in result.message


def test_release_protection_unparsable_workflow_is_unverified() -> None:
    result = _release_check(_gh_release(workflows={"bad.yaml": "on: [\n"}))
    assert result.passed is None
    assert "could not be parsed" in result.message


def test_release_protection_environments_unreadable_skips() -> None:
    result = _release_check(_gh_release(fail="/environments"))
    assert result.passed is None
    assert "Could not list environments" in result.message


def test_release_protection_workflows_unreadable_skips() -> None:
    result = _release_check(_gh_release(fail="graphql"))
    assert result.passed is None
    assert ".github/workflows" in result.message


def test_release_protection_definite_gap_outranks_unverified() -> None:
    """A gap tend can prove is reported even when something else was unreadable
    — otherwise one broken endpoint would mask a live escalation."""
    result = _release_check(
        _gh_release(
            environments=[
                {
                    "name": "pypi",
                    "deployment_branch_policy": None,
                    "protection_rules": [],
                }
            ],
            workflows={"release.yaml": _RELEASE_WORKFLOW, "bad.yaml": "on: [\n"},
        )
    )
    assert result.passed is False


def test_pattern_covers() -> None:
    assert _pattern_covers("~ALL", "refs/tags/v*")
    assert _pattern_covers("refs/tags/v*", "refs/tags/v*")
    assert _pattern_covers("refs/tags/*", "refs/tags/v1.0")
    assert not _pattern_covers("refs/tags/v*", "refs/tags/*")
    # fnmatch's `*` spans `/` where GitHub's does not, so a `**` inner pattern
    # is only covered by an outer that reaches as far.
    assert not _pattern_covers("refs/tags/*", "refs/tags/**")
    assert _pattern_covers("~ALL", "refs/tags/**")


def test_release_protection_unreadable_tag_ruleset_is_unverified() -> None:
    """A tag ruleset whose detail is withheld might carry the missing rule, so
    "absent" cannot be told from "not visible here"."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset(ruleset_id=11)],
            unreadable_ruleset_ids={11},
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is None


def test_release_protection_readable_ruleset_gates_despite_unreadable_sibling() -> None:
    """One ruleset the bot cannot bypass is enough; an unreadable sibling does
    not turn a proven gate into an unknown."""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset(ruleset_id=11), _tag_ruleset(ruleset_id=12)],
            unreadable_ruleset_ids={12},
            workflows={"release.yaml": _RELEASE_WORKFLOW},
        )
    )
    assert result.passed is True


def test_release_protection_reusable_caller_job_is_not_ungated_oidc() -> None:
    """A caller job's `permissions:` only caps what the called workflow may
    request; the environment is declared over there, and parsed there."""
    caller = """
on:
  push:
    tags: ["v*"]
jobs:
  call:
    uses: ./.github/workflows/publish.yaml
    permissions:
      id-token: write
"""
    callee = """
on:
  workflow_call:
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - run: echo publish
"""
    result = _release_check(
        _gh_release(
            environments=_tag_pinned_env(),
            policies={"pypi": [{"name": "v*", "type": "tag"}]},
            rulesets=[_tag_ruleset()],
            workflows={"release.yaml": caller, "publish.yaml": callee},
        )
    )
    assert result.passed is True
