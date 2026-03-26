"""Security checks for tend setup.

Verifies the repository has the security prerequisites described in
docs/security-model.md: branch protection on configured branches, bot
permission level, and required secrets.

Uses the `gh` CLI for GitHub API access. Checks degrade gracefully when
gh is unavailable or the token lacks permission.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
from dataclasses import dataclass

from tend.config import Config


@dataclass
class CheckResult:
    name: str
    passed: bool | None  # None = skipped/error
    message: str


def _gh(
    *args: str, input: str | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run a gh CLI command. Returns None if gh is not installed."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        return subprocess.run(
            [gh, *args],
            capture_output=True,
            text=True,
            timeout=30,
            input=input,
        )
    except subprocess.TimeoutExpired:
        return None


def detect_repo() -> str | None:
    """Detect owner/repo from the gh CLI context."""
    result = _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    if result and result.returncode == 0:
        repo = result.stdout.strip()
        return repo or None
    return None


def detect_default_branch(repo: str) -> str | None:
    """Detect the default branch for a repo via the GitHub API."""
    result = _gh("api", f"repos/{repo}", "--jq", ".default_branch")
    if result and result.returncode == 0:
        branch = result.stdout.strip()
        return branch or None
    return None


def check_branch_protection(
    repo: str, branch: str, *, default_branch: str | None = None
) -> CheckResult:
    """Check if a branch is protected against bot merges.

    Checks both that the branch is protected and that the protection actually
    prevents the bot from merging (via required reviews or a restrict-updates
    ruleset). Pass ``default_branch`` so ``~DEFAULT_BRANCH`` ruleset patterns
    can be resolved.
    """
    name = f"branch-protection:{branch}"
    result = _gh("api", f"repos/{repo}/branches/{branch}", "--jq", ".protected")
    if result is None:
        return CheckResult(name, None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(name, None, f"API error: {result.stderr.strip()}")

    if result.stdout.strip() != "true":
        return CheckResult(
            name,
            False,
            f"Branch '{branch}' is NOT protected. "
            "The bot must not be able to merge PRs — this is the primary security boundary. "
            "Add a branch protection rule or ruleset. See docs/security-model.md.",
        )

    # Branch is protected — now check if the bot can still merge.
    # A restrict-updates ruleset is sufficient (and preferred).
    if _has_restrict_updates_ruleset(repo, branch, default_branch=default_branch):
        return CheckResult(
            name,
            True,
            f"Branch '{branch}' is protected (restrict-updates ruleset)",
        )

    # Fall back to checking branch protection rules for required reviews.
    prot = _gh("api", f"repos/{repo}/branches/{branch}/protection")
    if prot is None or prot.returncode != 0:
        # Can't read details — branch is protected, assume OK.
        return CheckResult(name, True, f"Branch '{branch}' is protected")

    try:
        data = json.loads(prot.stdout)
    except json.JSONDecodeError:
        return CheckResult(name, True, f"Branch '{branch}' is protected")

    if not isinstance(data, dict):
        return CheckResult(name, True, f"Branch '{branch}' is protected")

    reviews = data.get("required_pull_request_reviews")
    if reviews and reviews.get("required_approving_review_count", 0) > 0:
        return CheckResult(
            name,
            True,
            f"Branch '{branch}' is protected (requires reviews)",
        )

    return CheckResult(
        name,
        False,
        f"Branch '{branch}' is protected but the bot can still merge PRs "
        f"(required_approving_review_count is 0 and no restrict-updates ruleset found). "
        "Either require at least 1 approving review, or add a 'Restrict updates' "
        "ruleset with only admins bypassing. See docs/security-model.md.",
    )


def _has_restrict_updates_ruleset(
    repo: str, branch: str, *, default_branch: str | None = None
) -> bool:
    """Check if any active ruleset restricts updates to the branch.

    Fetches rulesets and checks whether their ``conditions.ref_name.include``
    patterns actually cover ``branch``. Supports ``~DEFAULT_BRANCH``, ``~ALL``,
    exact refs, and fnmatch glob patterns.
    """
    result = _gh(
        "api",
        f"repos/{repo}/rulesets",
        "--jq",
        '[.[] | select(.enforcement == "active" and .target == "branch" and (.rules[]? | .type == "update"))]',
    )
    if result is None or result.returncode != 0:
        return False
    try:
        rulesets = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return False

    ref = f"refs/heads/{branch}"
    for ruleset in rulesets:
        conditions = ruleset.get("conditions") or {}
        ref_name = conditions.get("ref_name") or {}
        include = ref_name.get("include") or []
        exclude = ref_name.get("exclude") or []
        if _ref_matches_patterns(
            ref, include, default_branch
        ) and not _ref_matches_patterns(ref, exclude, default_branch):
            return True
    return False


def _ref_matches_patterns(
    ref: str, patterns: list[str], default_branch: str | None
) -> bool:
    """Check if a ref matches any GitHub ruleset pattern."""
    for pattern in patterns:
        if pattern == "~ALL":
            return True
        if pattern == "~DEFAULT_BRANCH":
            if default_branch and ref == f"refs/heads/{default_branch}":
                return True
            continue
        if fnmatch.fnmatch(ref, pattern):
            return True
    return False


def check_bot_permission(repo: str, bot_name: str) -> CheckResult:
    """Check the bot's permission level (should be write, not admin)."""
    result = _gh(
        "api",
        f"repos/{repo}/collaborators/{bot_name}/permission",
        "--jq",
        ".permission",
    )
    if result is None:
        return CheckResult("bot-permission", None, "gh CLI not found")
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Not Found" in stderr or "404" in stderr:
            return CheckResult(
                "bot-permission",
                None,
                f"Bot '{bot_name}' not found as a collaborator — check the bot_name in config",
            )
        return CheckResult(
            "bot-permission", None, "Could not check (may require admin access to read)"
        )

    perm = result.stdout.strip()
    if perm == "admin":
        return CheckResult(
            "bot-permission",
            False,
            f"Bot '{bot_name}' has admin permission — it can bypass branch protection. "
            "Downgrade to write access.",
        )
    return CheckResult(
        "bot-permission", True, f"Bot '{bot_name}' has '{perm}' permission"
    )


def check_secrets(repo: str, expected: list[str]) -> CheckResult:
    """Check that required secrets exist (repo-level, then org-level fallback)."""
    result = _gh("api", f"repos/{repo}/actions/secrets", "--jq", "[.secrets[].name]")
    if result is None:
        return CheckResult("secrets", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            "secrets", None, "Could not list secrets (may require admin access)"
        )

    try:
        secret_names = set(json.loads(result.stdout))
    except json.JSONDecodeError:
        return CheckResult("secrets", None, "Could not parse secrets response")

    missing = [s for s in expected if s not in secret_names]

    # Try org secrets for anything not found at repo level.
    org_forbidden = False
    if missing:
        org = repo.split("/")[0] if "/" in repo else None
        if org:
            org_secrets, org_forbidden = _list_org_secrets(org)
            if org_secrets is not None:
                still_missing = [s for s in missing if s not in org_secrets]
                found_at_org = [s for s in missing if s in org_secrets]
                if found_at_org and not still_missing:
                    return CheckResult(
                        "secrets",
                        True,
                        f"Required secrets present (org-level: {', '.join(found_at_org)})",
                    )
                if found_at_org:
                    missing = still_missing

    if missing:
        msg = (
            f"Missing secrets: {', '.join(missing)}. "
            "Add them in repo Settings > Secrets and variables > Actions."
        )
        if org_forbidden:
            msg += (
                "\nNote: Could not check org-level secrets (HTTP 403). "
                "If these secrets are set at the org level, grant the "
                "admin:org scope: gh auth refresh -h github.com -s admin:org"
            )
        return CheckResult("secrets", False, msg)
    return CheckResult(
        "secrets", True, f"Required secrets present: {', '.join(expected)}"
    )


def _list_org_secrets(org: str) -> tuple[set[str] | None, bool]:
    """List org-level secret names. Returns (secrets, permission_denied)."""
    result = _gh("api", f"orgs/{org}/actions/secrets", "--jq", "[.secrets[].name]")
    if result is None:
        return None, False
    if result.returncode != 0:
        forbidden = "HTTP 403" in result.stderr
        return None, forbidden
    try:
        return set(json.loads(result.stdout)), False
    except (json.JSONDecodeError, TypeError):
        return None, False


def check_repo_secret_allowlist(repo: str, allowed: set[str]) -> CheckResult:
    """Check that secrets available to workflows are in the allowlist.

    Checks repo-level secrets (always) and org-level secrets (best-effort).
    Any secret not in the allowlist is flagged — this catches release secrets
    (registry tokens, signing keys) that should be in a protected GitHub
    Environment instead.
    """
    result = _gh("api", f"repos/{repo}/actions/secrets", "--jq", "[.secrets[].name]")
    if result is None:
        return CheckResult("repo-secret-allowlist", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            "repo-secret-allowlist",
            None,
            "Could not list secrets (may require admin access)",
        )

    try:
        repo_secrets = set(json.loads(result.stdout))
    except json.JSONDecodeError:
        return CheckResult(
            "repo-secret-allowlist", None, "Could not parse secrets response"
        )

    # Best-effort: include org-level secrets (also available to workflows).
    org = repo.split("/")[0] if "/" in repo else None
    org_secrets: set[str] = set()
    org_forbidden = False
    if org:
        fetched, org_forbidden = _list_org_secrets(org)
        if fetched is not None:
            org_secrets = fetched

    unexpected_repo = sorted(repo_secrets - allowed)
    unexpected_org = sorted(org_secrets - allowed - repo_secrets)

    if unexpected_repo or unexpected_org:
        parts = []
        if unexpected_repo:
            parts.append(f"repo-level: {', '.join(unexpected_repo)}")
        if unexpected_org:
            parts.append(f"org-level: {', '.join(unexpected_org)}")
        return CheckResult(
            "repo-secret-allowlist",
            False,
            f"Unexpected secrets ({'; '.join(parts)}). "
            "These are available to all workflows, including those triggered "
            "by PRs. Move release secrets to a protected environment. "
            "If intentionally available, add to secrets.allowed "
            "in .config/tend.toml. See docs/security-model.md.",
        )

    msg = "All secrets available to workflows are in allowlist"
    if org_forbidden:
        msg += " (could not check org-level — grant admin:org scope to verify)"
    return CheckResult("repo-secret-allowlist", True, msg)


def _restrict_updates_ruleset(extra_branches: list[str]) -> str:
    """Build the JSON body for a restrict-updates ruleset.

    Always includes ~DEFAULT_BRANCH. Extra branches are added as
    refs/heads/<name> patterns.
    """
    include = ["~DEFAULT_BRANCH"] + [f"refs/heads/{b}" for b in extra_branches]
    return json.dumps(
        {
            "name": "Merge access",
            "target": "branch",
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": include,
                    "exclude": [],
                }
            },
            "rules": [{"type": "update"}],
            "bypass_actors": [
                {
                    "actor_id": 5,
                    "actor_type": "RepositoryRole",
                    "bypass_mode": "exempt",
                }
            ],
        }
    )


def fix_branch_protection(
    repo: str,
    default_branch: str,
    extra_branches: list[str] | None = None,
) -> CheckResult:
    """Create a restrict-updates ruleset covering protected branches.

    Always covers the default branch. Extra branches from config are included
    in the same ruleset. Only admins (actor_id 5) can bypass.
    """
    extra = [b for b in (extra_branches or []) if b != default_branch]
    body = _restrict_updates_ruleset(extra)
    result = _gh(
        "api",
        f"repos/{repo}/rulesets",
        "--method",
        "POST",
        "--input",
        "-",
        input=body,
    )
    name = f"branch-protection:{default_branch}"
    if result is None:
        return CheckResult(name, None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            name,
            False,
            f"Failed to create ruleset: {result.stderr.strip()}",
        )
    branches = [default_branch] + extra
    return CheckResult(
        name,
        True,
        f"Created 'Merge access' ruleset — only admins can merge ({', '.join(branches)})",
    )


def run_all_checks(cfg: Config, repo: str | None = None) -> list[CheckResult]:
    """Run all security checks. Auto-detects repo if not provided."""
    if shutil.which("gh") is None:
        return [
            CheckResult(
                "prerequisites",
                None,
                "gh CLI not found — install it to run security checks",
            )
        ]

    if repo is None:
        repo = detect_repo()
    if repo is None:
        return [
            CheckResult(
                "prerequisites",
                None,
                "Could not detect repository. Run from a git repo with a GitHub remote, or pass --repo.",
            )
        ]

    default_branch = detect_default_branch(repo)
    if default_branch is None:
        return [
            CheckResult(
                "prerequisites", None, f"Could not detect default branch for {repo}"
            )
        ]

    allowed = {cfg.bot_token_secret, cfg.claude_token_secret} | set(
        cfg.allowed_repo_secrets
    )

    results = [
        check_branch_protection(repo, default_branch, default_branch=default_branch)
    ]
    for branch in cfg.protected_branches:
        if branch != default_branch:
            results.append(
                check_branch_protection(repo, branch, default_branch=default_branch)
            )
    results.append(check_bot_permission(repo, cfg.bot_name))
    results.append(check_secrets(repo, [cfg.bot_token_secret, cfg.claude_token_secret]))
    results.append(check_repo_secret_allowlist(repo, allowed))
    return results
