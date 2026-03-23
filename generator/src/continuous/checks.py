"""Security checks for continuous setup.

Verifies the repository has the security prerequisites described in
docs/security-model.md: branch protection on the default branch, bot
permission level, and required secrets.

Uses the `gh` CLI for GitHub API access. Checks degrade gracefully when
gh is unavailable or the token lacks permission.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from continuous.config import Config


@dataclass
class CheckResult:
    name: str
    passed: bool | None  # None = skipped/error
    message: str


def _gh(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run a gh CLI command. Returns None if gh is not installed."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        return subprocess.run(
            [gh, *args], capture_output=True, text=True, timeout=30
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


def check_branch_protection(repo: str, branch: str) -> CheckResult:
    """Check if the default branch is protected."""
    result = _gh("api", f"repos/{repo}/branches/{branch}", "--jq", ".protected")
    if result is None:
        return CheckResult("branch-protection", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult("branch-protection", None, f"API error: {result.stderr.strip()}")

    if result.stdout.strip() == "true":
        return CheckResult("branch-protection", True, f"Default branch '{branch}' is protected")
    return CheckResult(
        "branch-protection",
        False,
        f"Default branch '{branch}' is NOT protected. "
        "The bot must not be able to merge PRs — this is the primary security boundary. "
        "Add a branch protection rule or ruleset. See docs/security-model.md.",
    )


def check_bot_permission(repo: str, bot_name: str) -> CheckResult:
    """Check the bot's permission level (should be write, not admin)."""
    result = _gh("api", f"repos/{repo}/collaborators/{bot_name}/permission", "--jq", ".permission")
    if result is None:
        return CheckResult("bot-permission", None, "gh CLI not found")
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Not Found" in stderr or "404" in stderr:
            return CheckResult(
                "bot-permission", None,
                f"Bot '{bot_name}' not found as a collaborator — check the bot_name in config",
            )
        return CheckResult("bot-permission", None, "Could not check (may require admin access to read)")

    perm = result.stdout.strip()
    if perm == "admin":
        return CheckResult(
            "bot-permission",
            False,
            f"Bot '{bot_name}' has admin permission — it can bypass branch protection. "
            "Downgrade to write access.",
        )
    return CheckResult("bot-permission", True, f"Bot '{bot_name}' has '{perm}' permission")


def check_secrets(repo: str, expected: list[str]) -> CheckResult:
    """Check that required secrets exist in the repository."""
    result = _gh("api", f"repos/{repo}/actions/secrets", "--jq", "[.secrets[].name]")
    if result is None:
        return CheckResult("secrets", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult("secrets", None, "Could not list secrets (may require admin access)")

    try:
        secret_names = json.loads(result.stdout)
    except json.JSONDecodeError:
        return CheckResult("secrets", None, "Could not parse secrets response")

    missing = [s for s in expected if s not in secret_names]
    if missing:
        return CheckResult(
            "secrets",
            False,
            f"Missing secrets: {', '.join(missing)}. "
            "Add them in repo Settings > Secrets and variables > Actions.",
        )
    return CheckResult("secrets", True, f"Required secrets present: {', '.join(expected)}")


def run_all_checks(cfg: Config, repo: str | None = None) -> list[CheckResult]:
    """Run all security checks. Auto-detects repo if not provided."""
    if shutil.which("gh") is None:
        return [CheckResult("prerequisites", None, "gh CLI not found — install it to run security checks")]

    if repo is None:
        repo = detect_repo()
    if repo is None:
        return [CheckResult(
            "prerequisites",
            None,
            "Could not detect repository. Run from a git repo with a GitHub remote, or pass --repo.",
        )]

    return [
        check_branch_protection(repo, cfg.default_branch),
        check_bot_permission(repo, cfg.bot_name),
        check_secrets(repo, [cfg.bot_token_secret, cfg.claude_token_secret]),
    ]
