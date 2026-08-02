"""Security checks for tend setup.

Verifies the repository has the security prerequisites described in
docs/security-model.md: branch protection on configured branches, bot
permission level, and required secrets.

Uses the `gh` CLI for GitHub API access. Checks degrade gracefully when
gh is unavailable or the token lacks permission.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from tend.config import Config
from tend.workflows import TEND_ENVIRONMENT


# GitHub's base repository role IDs, as they appear in a ruleset's
# `bypass_actors`. The IDs are not ordered by privilege — maintain (2) sits
# below write (4) — so the plausible guess for "maintain" is in fact the bot's
# own role, and guessing it into a bypass list hands the bot the merge. The API
# reports only the number; GraphQL names it, so verify against a live ruleset:
#
#   gh api graphql -f query='{repository(owner:"OWNER", name:"REPO")
#     {rulesets(first:10){nodes{name bypassActors(first:10)
#     {nodes{repositoryRoleDatabaseId repositoryRoleName}}}}}}'
ROLE_ID_MAINTAIN = 2
ROLE_ID_WRITE = 4
ROLE_ID_ADMIN = 5

# The roles above the bot's write access, so the only ones a merge
# restriction's `bypass_actors` may grant.
BYPASS_ROLE_IDS = frozenset({ROLE_ID_MAINTAIN, ROLE_ID_ADMIN})

# Non-role bypass actors that also unambiguously outrank a write-access bot. A
# `User` actor is resolved against the bot's own id; the rest (Team,
# Integration, DeployKey) name a principal whose membership isn't visible from
# the ruleset, so the bot can't be ruled out.
BYPASS_ACTOR_TYPES_ABOVE_BOT = frozenset({"OrganizationAdmin", "EnterpriseOwner"})


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


def detect_canonical_owner() -> str | None:
    """Detect the *canonical* owner of the repo this directory is associated with.

    Tend's generated workflows are committed and shipped to the canonical
    repo, so the fork guard string must match the canonical owner — not
    whoever happens to be running `tend init` from a fork.

    `gh repo view` resolves the directory's default repo (already canonical
    when `upstream` is configured or `gh repo set-default` set). Then a
    single `gh api repos/<owner>/<name>` call returns `.fork`, `.owner.login`,
    and `.source.owner.login` — `source` is the *root* canonical, so chained
    forks (alice → bob → canonical) resolve correctly in one call.

    Returns None when `gh` is unavailable or either call fails. Callers
    treat that as "skip the guard"; we never silently ship a fork owner
    in the guard string.
    """
    repo = detect_repo()
    if repo is None:
        return None
    result = _gh("api", f"repos/{repo}")
    if not result or result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    if data["fork"]:
        return data["source"]["owner"]["login"]
    return data["owner"]["login"]


def detect_default_branch(repo: str) -> str | None:
    """Detect the default branch for a repo via the GitHub API."""
    result = _gh("api", f"repos/{repo}", "--jq", ".default_branch")
    if result and result.returncode == 0:
        branch = result.stdout.strip()
        return branch or None
    return None


def check_branch_protection(repo: str, branch: str, bot_name: str) -> CheckResult:
    """Check if a branch is protected against bot merges.

    Checks both that the branch is protected and that the protection actually
    prevents the bot from merging (via required reviews or a restrict-updates
    ruleset).
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
    ruleset = _has_restrict_updates_ruleset(repo, branch, bot_name)
    if ruleset is True:
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

    # Neither required reviews nor a confirmed restrict-updates ruleset.
    if ruleset is None:
        # Ruleset check was inconclusive — don't false-positive.
        return CheckResult(
            name,
            None,
            f"Branch '{branch}' is protected but could not verify that the bot "
            "cannot bypass its rulesets — either they aren't readable with this "
            "token, or a bypass actor names a team, app, or deploy key whose "
            "membership isn't resolvable here. Check the bypass list manually.",
        )

    return CheckResult(
        name,
        False,
        f"Branch '{branch}' is protected but the bot can still merge PRs "
        "(required_approving_review_count is 0, and no restrict-updates ruleset "
        "the bot cannot bypass). Either require at least 1 approving review, or "
        "add a 'Restrict updates' ruleset whose bypass actors are all above write. "
        "See docs/security-model.md.",
    )


def _user_id(login: str) -> int | None:
    """The numeric GitHub user id for a login, which is how a `User` bypass
    actor names its principal."""
    result = _gh("api", f"users/{login}", "--jq", ".id")
    if result is None or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _ruleset_blocks_bot(repo: str, ruleset_id: int, bot_name: str) -> bool | None:
    """Whether a ruleset's bypass list keeps a write-access bot out.

    The repo-scoped endpoint serves organization- and enterprise-sourced
    rulesets too, so any applying ruleset can be fetched here.

    Returns True if every bypass actor outranks the bot, False if one of them
    is the bot itself or a role at write or below, None if the ruleset can't be
    verified: unreadable, a bypass list GitHub withholds (only ruleset admins
    see `bypass_actors`), or a principal this can't resolve (a team, app, or
    deploy key).
    """
    result = _gh("api", f"repos/{repo}/rulesets/{ruleset_id}")
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    actors = data.get("bypass_actors")
    if actors is None:
        return None
    # A user exemption is decidable: the bot's login resolves to the id the
    # actor names. Naming the bot is the worst case — an explicit grant of the
    # merge the restriction exists to deny.
    bot_id = None
    if any(a.get("actor_type") == "User" for a in actors):
        bot_id = _user_id(bot_name)

    unresolved = False
    for actor in actors:
        actor_type = actor.get("actor_type")
        if actor_type == "RepositoryRole":
            if actor.get("actor_id") not in BYPASS_ROLE_IDS:
                return False
        elif actor_type == "User":
            if bot_id is None:
                unresolved = True
            elif actor.get("actor_id") == bot_id:
                return False
        elif actor_type not in BYPASS_ACTOR_TYPES_ABOVE_BOT:
            unresolved = True
    return None if unresolved else True


def _has_restrict_updates_ruleset(repo: str, branch: str, bot_name: str) -> bool | None:
    """Check if an active ruleset stops the bot updating the branch.

    An `update` rule alone isn't enough — a bypass actor at write or below
    defeats it, and write is exactly what the bot holds. So each update rule is
    followed back to its ruleset and its bypass list checked.

    Returns True if found, False if confirmed absent or bypassable, None if
    unable to check.

    Uses the per-branch rules endpoint which resolves patterns like
    ~DEFAULT_BRANCH.
    """
    result = _gh("api", f"repos/{repo}/rules/branches/{branch}")
    if result is None or result.returncode != 0:
        return None
    try:
        rules = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(rules, list):
        return None

    update_rules = [r for r in rules if r.get("type") == "update"]
    if not update_rules:
        return False

    # Several rulesets can contribute an update rule; one the bot can't bypass
    # is enough to protect the branch. A rule we can't trace back to its
    # ruleset is unverified, not absent.
    unresolved = False
    for rule in update_rules:
        ruleset_id = rule.get("ruleset_id")
        verdict = (
            _ruleset_blocks_bot(repo, ruleset_id, bot_name)
            if ruleset_id is not None
            else None
        )
        if verdict is True:
            return True
        unresolved = unresolved or verdict is None
    return None if unresolved else False


def check_bot_permission(repo: str, bot_name: str) -> CheckResult:
    """Check the bot's effective access stays at write or below.

    Reads the `permissions` booleans: they report effective capabilities, so a
    custom role built on maintain or admin fails the same as the base role.
    Neither string field works — the legacy `.permission` reports a
    maintain-role collaborator as "write" (and maintain bypasses the merge
    restriction), while matching `.role_name` against base-role names would
    pass any custom role whatever it grants.
    """
    result = _gh("api", f"repos/{repo}/collaborators/{bot_name}/permission")
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

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return CheckResult(
            "bot-permission", None, "Could not parse permission response"
        )

    perms = data["user"]["permissions"]
    role = data["role_name"]
    if perms["admin"] or perms["maintain"]:
        return CheckResult(
            "bot-permission",
            False,
            f"Bot '{bot_name}' has {role} permission — it can bypass branch protection. "
            "Downgrade to write access.",
        )
    return CheckResult(
        "bot-permission", True, f"Bot '{bot_name}' has '{role}' permission"
    )


# The operational secrets live in a deployment-gated environment rather than at
# repo level, so every "is the secret set?" check reads them from there. A copy
# left at repo level defeats the gate entirely — any workflow can read it
# without naming the environment — and that is what `check_repo_secret_allowlist`
# now catches, since the operational names are no longer in its allowed set.
def _env_secret_names(repo: str) -> tuple[set[str] | None, str]:
    """Secret names in the tend environment. Returns (names, error message)."""
    result = _gh(
        "api",
        f"repos/{repo}/environments/{TEND_ENVIRONMENT}/secrets",
        "--jq",
        "[.secrets[].name]",
    )
    if result is None:
        return None, "gh CLI not found"
    if result.returncode != 0:
        return None, (
            f"Could not list secrets in the '{TEND_ENVIRONMENT}' environment "
            "(missing environment, or requires admin access). "
            "See the environment check below for how to create it."
        )
    try:
        return set(json.loads(result.stdout)), ""
    except json.JSONDecodeError:
        return None, "Could not parse environment secrets response"


def check_environment(repo: str, admitted: list[str]) -> CheckResult:
    """The environment exists and admits only the refs the bot cannot write.

    This is the whole mechanism: a job naming the environment runs only from a
    ref in its deployment branch policy, so a workflow pushed to a feature
    branch is refused before its first step. A policy that admits anything the
    bot can push gives the secrets back.
    """
    name = "environment"
    result = _gh("api", f"repos/{repo}/environments/{TEND_ENVIRONMENT}")
    if result is None:
        return CheckResult(name, None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' not found. The operational "
            "secrets must live in it, gated to admin-only refs, or a workflow "
            "pushed to any branch can read them:\n"
            f"  gh api -X PUT repos/{repo}/environments/{TEND_ENVIRONMENT} "
            '--input - <<< \'{"deployment_branch_policy":'
            '{"protected_branches":false,"custom_branch_policies":true}}\'\n'
            f"  gh api -X POST repos/{repo}/environments/{TEND_ENVIRONMENT}"
            "/deployment-branch-policies --input - <<< "
            f'\'{{"name":"{admitted[0]}","type":"branch"}}\'\n'
            "Then move each secret into the environment and delete the "
            "repo-level copy.",
        )
    try:
        env = json.loads(result.stdout)
    except json.JSONDecodeError:
        return CheckResult(name, None, "Could not parse environment response")

    policy = env.get("deployment_branch_policy")
    if not policy:
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' has no deployment branch policy, "
            "so every ref reaches its secrets — including a branch the bot pushes.",
        )
    if policy.get("protected_branches"):
        # "Protected branches" keys on whether a ruleset covers the branch, not
        # on who may push it, so a branch the bot can push while a ruleset
        # merely requires reviews would be admitted. Only a named list is
        # verifiable from here.
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' admits all protected branches. "
            "Use a custom branch policy naming the default branch and any "
            "protected_branches, so the admitted set is the one tend verifies.",
        )

    listed = _gh(
        "api",
        f"repos/{repo}/environments/{TEND_ENVIRONMENT}/deployment-branch-policies",
        "--jq",
        "[.branch_policies[].name]",
    )
    if listed is None or listed.returncode != 0:
        return CheckResult(name, None, "Could not list deployment branch policies")
    try:
        names = set(json.loads(listed.stdout))
    except json.JSONDecodeError:
        return CheckResult(name, None, "Could not parse branch policy response")

    extra = names - set(admitted)
    if extra:
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' admits {', '.join(sorted(extra))}, "
            "which tend does not verify the bot is kept off. Restrict the policy "
            f"to: {', '.join(admitted)}.",
        )
    if not names:
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' admits no refs — every tend "
            "workflow will be refused before its first step.",
        )
    return CheckResult(
        name,
        True,
        f"Environment '{TEND_ENVIRONMENT}' admits only {', '.join(sorted(names))}",
    )


def check_secrets(repo: str, expected: list[str]) -> CheckResult:
    """Check that required secrets exist (repo-level, then org-level fallback)."""
    secret_names, err = _env_secret_names(repo)
    if secret_names is None:
        return CheckResult("secrets", None, err)

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
            "in .config/tend.yaml. See docs/security-model.md.",
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
                    "actor_id": ROLE_ID_ADMIN,
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
    in the same ruleset. Only admins can bypass.
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

    # The engine-specific auth secret is verified by check_claude_auth /
    # check_codex_auth below, which name the relevant one in their message.
    required_secrets = [cfg.bot_token_secret]

    # The operational secrets are deliberately absent from `allowed`: they
    # belong to the environment, and a copy left at repo level is readable by
    # any workflow the bot can push, which is exactly the hole the environment
    # closes. The allowlist check therefore flags them as unexpected.
    allowed = set(cfg.allowed_repo_secrets)

    # Every ref the environment may admit is one the bot cannot write: the
    # default branch and the protected branches, both covered by the merge
    # restriction checks above.
    admitted = [
        default_branch,
        *(b for b in cfg.protected_branches if b != default_branch),
    ]

    results = [check_branch_protection(repo, default_branch, cfg.bot_name)]
    for branch in cfg.protected_branches:
        if branch != default_branch:
            results.append(check_branch_protection(repo, branch, cfg.bot_name))
    results.append(check_bot_permission(repo, cfg.bot_name))
    results.append(check_environment(repo, admitted))
    results.append(check_secrets(repo, required_secrets))
    if cfg.harness == "claude":
        results.append(check_claude_auth(repo, cfg))
    else:
        results.append(check_codex_auth(repo, cfg))
    results.append(check_repo_secret_allowlist(repo, allowed))
    return results


def check_claude_auth(repo: str, cfg: Config) -> CheckResult:
    """Claude needs either CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY —
    both being absent is the failure mode. Both being set is fine; the
    action prefers the OAuth token.
    """
    names, err = _env_secret_names(repo)
    if names is None:
        return CheckResult("claude-auth", None, err)
    has_oauth = cfg.claude_token_secret in names
    has_key = cfg.anthropic_api_key_secret in names
    if has_oauth or has_key:
        which = []
        if has_oauth:
            which.append(cfg.claude_token_secret)
        if has_key:
            which.append(cfg.anthropic_api_key_secret)
        return CheckResult(
            "claude-auth", True, f"Claude auth secret present: {', '.join(which)}"
        )
    return CheckResult(
        "claude-auth",
        False,
        f"Claude harness selected but neither {cfg.claude_token_secret} nor "
        f"{cfg.anthropic_api_key_secret} is set in the '{TEND_ENVIRONMENT}' environment.",
    )


def check_codex_auth(repo: str, cfg: Config) -> CheckResult:
    """Codex needs OPENAI_API_KEY — absence is the failure mode. The
    subscription auth.json path is not supported.
    """
    names, err = _env_secret_names(repo)
    if names is None:
        return CheckResult("codex-auth", None, err)
    if cfg.openai_key_secret in names:
        return CheckResult(
            "codex-auth",
            True,
            f"Codex auth secret present: {cfg.openai_key_secret}",
        )
    return CheckResult(
        "codex-auth",
        False,
        f"Codex harness selected but {cfg.openai_key_secret} "
        "is not set as a repo secret.",
    )
