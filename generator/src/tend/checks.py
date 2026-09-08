"""Security checks for tend setup.

Verifies the boundaries docs/security-model.md claims: the configured merge
policy is exact, the yolo control plane remains maintainer-owned, extra protected
branches and future releases are immutable, and a run the bot can cause
reaches no unintended credential (the `tend` environment's deployment branch
policy, every other credential-holding environment's gate, the operational
secrets living in the environment, and no repo-level secret outside the
allowlist).

Uses the `gh` CLI for GitHub API access. Checks degrade gracefully when
gh is unavailable or the token lacks permission. Everything read here is
readable with the bot's own write-scoped token, so the nightly run sees
the same answers a maintainer does — with one asymmetry: a ruleset's
`bypass_actors` list is served only to repo admins, but every response
carries `current_user_can_bypass`, GitHub's own evaluation of the caller
against that list. A run as the bot reads its verdict there; a run as an
admin reads the list; a token that is neither — or a failed read, or a
listed principal tend cannot resolve — reports unknown.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from dataclasses import dataclass, replace
from functools import cache
from urllib.parse import quote

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from tend.config import (
    ANTHROPIC_API_KEY_SECRET,
    BOT_TOKEN_SECRET,
    CLAUDE_TOKEN_SECRET,
    CODEX_AUTH_SECRET,
    CODEX_REFRESH_AUTH_SECRET,
    CODEX_REFRESH_PAT_SECRET,
    MEMORY_GIST_SECRET,
    OPENAI_KEY_SECRET,
    Config,
)
from tend.workflows import (
    CODEOWNERS_BEGIN,
    CODEOWNERS_END,
    CONTROL_PLANE_PATHS,
    TEND_ENVIRONMENT,
    generate_all,
)

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

# Triggers a write-scoped actor can both fire *and* steer — it decides not only
# that the run happens but what the run publishes. A deployment branch policy
# does not gate these, because the actor fires them at a ref the policy already
# admits; only a required reviewer does. Verified against live GitHub with a
# write-access (non-admin, non-bypass) collaborator:
#
#   - `release`: creating a release against an *existing* tag takes no tag
#     operation, so a tag ruleset does not stop it — and the release's body and
#     uploaded assets are the actor's own.
#   - `repository_dispatch`: the actor supplies `client_payload` wholesale.
#   - `workflow_dispatch` *with inputs* (added per workflow, not listed here):
#     the actor supplies the inputs.
#
# A `workflow_dispatch` with no inputs is deliberately absent, as are `push`,
# `create`, `pull_request`, `workflow_run`, `deployment` and `schedule`: each
# runs code fixed by the ref, so against an admin-gated ref the worst the actor
# achieves is re-publishing what an admin already published.
BOT_STEERABLE_TRIGGERS = frozenset({"release", "repository_dispatch"})

IMMUTABLE_RELEASES_API_VERSION = "2026-03-10"


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
            check=False,
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


def detect_canonical_owner(repo: str | None = None) -> str | None:
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
    repo = repo or detect_repo()
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


def check_immutable_releases(repo: str) -> CheckResult:
    """Check that future published releases and their tags cannot be rewritten."""
    result = _gh(
        "api",
        "-H",
        f"X-GitHub-Api-Version: {IMMUTABLE_RELEASES_API_VERSION}",
        f"repos/{repo}/immutable-releases",
    )
    if result is None:
        return CheckResult("immutable-releases", None, "gh CLI not found")
    if result.returncode != 0:
        if "HTTP 404" in result.stderr:
            return CheckResult(
                "immutable-releases",
                None,
                "Could not read the immutable-releases setting. Repository "
                "admin access is required to verify it.",
            )
        return CheckResult(
            "immutable-releases", None, f"API error: {result.stderr.strip()}"
        )
    try:
        enabled = json.loads(result.stdout)["enabled"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return CheckResult(
            "immutable-releases", None, "GitHub returned an unreadable response"
        )
    if enabled is True:
        return CheckResult(
            "immutable-releases",
            True,
            "Future published releases and their tags will be immutable.",
        )
    return CheckResult(
        "immutable-releases",
        False,
        "Immutable releases are disabled. A write-access bot can rewrite a "
        "published release's assets or notes. Run `tend check --fix`.",
    )


def check_tag_protection(repo: str, bot_name: str) -> CheckResult:
    """Check that the bot can neither create nor repoint a tag."""
    protected = _tags_admin_gated(repo, bot_name)
    if protected is None:
        return CheckResult(
            "tag-protection", None, "Could not verify the all-tags ruleset"
        )
    if protected:
        return CheckResult(
            "tag-protection",
            True,
            "All tag creation and updates require admin access.",
        )
    return CheckResult(
        "tag-protection",
        False,
        "The bot can create or repoint a tag. Run `tend check --fix` to "
        "create an admin-gated all-tags ruleset.",
    )


def check_branch_protection(
    repo: str,
    branch: str,
    bot_name: str,
    *,
    expected_bypass: str = "never",
) -> CheckResult:
    """Check that a branch gives the bot exactly the configured merge access.

    ``never`` is maintainer-only. ``pull_requests_only`` is yolo: the
    bot may merge through GitHub's pull-request API, but may not push the ref
    directly. Extra protected branches always use ``never``.
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
            f"Branch '{branch}' is NOT protected. Add the Tend ruleset before "
            "running the bot. See docs/security-model.md.",
        )

    bypass = update_ruleset_bypass(repo, branch, bot_name)
    if bypass == expected_bypass:
        lifecycle = {
            rule_type: _ruleset_type_bypass(repo, branch, bot_name, rule_type)
            for rule_type in ("creation", "deletion")
        }
        if any(value is None for value in lifecycle.values()):
            return CheckResult(
                name,
                None,
                f"Branch '{branch}' update access is correct, but its creation "
                "and deletion rules could not be verified.",
            )
        unrestricted = [
            rule_type
            for rule_type, value in lifecycle.items()
            if value in {"absent", "always"}
        ]
        if unrestricted:
            return CheckResult(
                name,
                False,
                f"Branch '{branch}' lets the bot {', '.join(unrestricted)} the "
                "ref. Tend protects creation, update, and deletion as one "
                "lifecycle.",
            )
        access = (
            "may merge pull requests but cannot push directly"
            if bypass == "pull_requests_only"
            else "cannot update the branch"
        )
        return CheckResult(
            name,
            True,
            f"Branch '{branch}' is protected; the bot {access}.",
        )

    if expected_bypass == "pull_requests_only":
        if bypass is None:
            return CheckResult(
                name,
                None,
                f"Branch '{branch}' is protected but the bot's effective "
                "ruleset bypass could not be verified.",
            )
        if bypass == "never":
            return CheckResult(
                name,
                False,
                f"Branch '{branch}' blocks the bot from merging pull requests; "
                "yolo requires a pull-request-only bypass.",
            )
        return CheckResult(
            name,
            False,
            f"Branch '{branch}' lets the bot push directly. Yolo requires a "
            "pull-request-only bypass.",
        )

    # A ruleset that positively grants the bot a bypass is authoritative. Do
    # not fall back to classic branch protection: rulesets layer on top of it,
    # and a pull-request-only bypass is exactly the yolo authority that a
    # switch back to maintainer mode must remove.
    if bypass in {"pull_requests_only", "always"}:
        return CheckResult(
            name,
            False,
            f"Branch '{branch}' still gives the bot a ruleset bypass. "
            "Maintainer mode requires removing that bypass.",
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
    if bypass is None:
        # Ruleset check was inconclusive — don't false-positive.
        return CheckResult(
            name,
            None,
            f"Branch '{branch}' is protected but could not verify that the bot "
            "cannot bypass its rulesets — a ruleset read failed, a bypass list "
            "is withheld (a repo admin reads one; the bot's own run reads "
            "GitHub's verdict on it without needing the list), or a bypass "
            "actor names a principal tend cannot resolve: a team, app, or "
            "deploy key, or any user if `bot_name` itself does not resolve to "
            "an account. Re-run as the bot or an admin, or check the bypass "
            "list manually.",
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


def _default_branch_file(repo: str, branch: str, path: str) -> str | None:
    """Read one file from the default branch, returning None when absent."""
    result = _gh(
        "api",
        f"repos/{repo}/contents/{path}?ref={quote(branch, safe='')}",
    )
    if result is None or result.returncode != 0:
        return None
    try:
        encoded = json.loads(result.stdout)["content"]
        return base64.b64decode(encoded).decode()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None


def check_control_plane_codeowners(
    repo: str, branch: str, owner: str, bot_name: str
) -> CheckResult:
    """Check that Tend's final CODEOWNERS block protects its control plane."""
    name = "control-plane-codeowners"
    if owner.casefold() == f"@{bot_name}".casefold():
        return CheckResult(
            name,
            False,
            "control_plane_owner is the Tend bot; yolo requires an independent "
            "GitHub user.",
        )
    for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        content = _default_branch_file(repo, branch, path)
        if content is None:
            continue
        lines = [CODEOWNERS_BEGIN]
        lines.extend(f"{protected} {owner}" for protected in CONTROL_PLANE_PATHS)
        lines.append(CODEOWNERS_END)
        block = "\n".join(lines)
        if (
            content.count(CODEOWNERS_BEGIN) == 1
            and content.count(CODEOWNERS_END) == 1
            and content.rstrip().endswith(block)
        ):
            begin_line = content.splitlines().index(CODEOWNERS_BEGIN) + 1
            errors_result = _gh(
                "api",
                f"repos/{repo}/codeowners/errors?ref={quote(branch, safe='')}",
            )
            if errors_result is None or errors_result.returncode != 0:
                return CheckResult(
                    name, None, "Could not verify GitHub's CODEOWNERS validation"
                )
            try:
                errors = json.loads(errors_result.stdout).get("errors", [])
            except (json.JSONDecodeError, AttributeError):
                return CheckResult(
                    name, None, "GitHub returned unreadable CODEOWNERS errors"
                )
            managed_lines = range(begin_line, begin_line + len(lines))
            managed_errors = [
                error
                for error in errors
                if not isinstance(error, dict)
                or error.get("line") in managed_lines
                or error.get("line") is None
            ]
            if managed_errors:
                return CheckResult(
                    name,
                    False,
                    "GitHub reports an error in Tend's generated CODEOWNERS block; "
                    "verify that control_plane_owner exists and has repository "
                    "write access.",
                )
            return CheckResult(
                name,
                True,
                f"{path} gives {owner} final ownership of Tend's control plane.",
            )
        return CheckResult(
            name,
            False,
            f"{path} does not end with Tend's generated control-plane ownership "
            "block. Run `tend init`, commit it, and merge it before `tend check "
            "--fix` enables yolo merge mode.",
        )
    return CheckResult(
        name,
        False,
        "No effective CODEOWNERS file exists on the default branch. Run `tend "
        "init`, commit it, and merge it before `tend check --fix` enables yolo "
        "merge mode.",
    )


def check_control_plane_ruleset(repo: str, branch: str, bot_name: str) -> CheckResult:
    """Check for a non-bypassable stale-dismissed CODEOWNERS review rule."""
    name = "control-plane-ruleset"
    result = _gh("api", f"repos/{repo}/rules/branches/{branch}")
    if result is None or result.returncode != 0:
        return CheckResult(name, None, "Could not list rules on the default branch")
    try:
        rules = json.loads(result.stdout)
    except json.JSONDecodeError:
        return CheckResult(name, None, "GitHub returned unreadable branch rules")
    if not isinstance(rules, list):
        return CheckResult(name, None, "GitHub returned unreadable branch rules")

    unresolved = False
    ruleset_ids = {
        rule.get("ruleset_id")
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("type") == "pull_request"
        and rule.get("ruleset_id") is not None
    }
    for ruleset_id in ruleset_ids:
        data = _fetch_ruleset(repo, ruleset_id)
        if data is None:
            unresolved = True
            continue
        bypass = _ruleset_bot_bypass(data, bot_name)
        if bypass is None:
            unresolved = True
            continue
        if bypass != "never":
            continue
        for rule in data.get("rules", []):
            parameters = rule.get("parameters", {})
            if (
                rule.get("type") == "pull_request"
                and parameters.get("require_code_owner_review") is True
                and parameters.get("dismiss_stale_reviews_on_push") is True
            ):
                return CheckResult(
                    name,
                    True,
                    "Control-plane changes require a fresh CODEOWNER approval "
                    "that the bot cannot bypass.",
                )
    if unresolved:
        return CheckResult(name, None, "Could not verify the control-plane ruleset")
    return CheckResult(
        name,
        False,
        "No active rule requires fresh, non-bypassable CODEOWNER approval. "
        "Run `tend check --fix` after the generated CODEOWNERS block is on the "
        "default branch.",
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


def _current_login() -> str | None:
    """The login the token authenticates as, or None when that can't be read.

    Uncached on purpose: a run makes at most a handful of these calls, and a
    module-level cache would leak between tests that repatch `_gh`.
    """
    result = _gh("api", "user", "--jq", ".login")
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _same_login(a: str, b: str) -> bool:
    """Casefolded equality is the identity test for logins — GitHub logins
    are case-insensitive and the config takes whatever case the maintainer
    typed."""
    return a.casefold() == b.casefold()


def _ruleset_bot_bypass(data: dict, bot_name: str) -> str | None:
    """The bot's bypass level for one ruleset.

    GitHub evaluates this directly when the caller is the bot. An admin sees
    the actor list instead, so Tend resolves user and repository-role entries
    against the bot's known write access. Unknown principals remain unknown.
    """
    current = data.get("current_user_can_bypass")
    if current in {"never", "pull_requests_only", "always", "exempt"}:
        login = _current_login()
        if login is not None and _same_login(login, bot_name):
            return "always" if current == "exempt" else current

    actors = data.get("bypass_actors")
    if actors is None:
        return None
    bot_id = None
    if any(actor.get("actor_type") == "User" for actor in actors):
        bot_id = _user_id(bot_name)

    applicable: list[str] = []
    unresolved = False
    for actor in actors:
        actor_type = actor.get("actor_type")
        actor_id = actor.get("actor_id")
        applies = False
        if actor_type == "RepositoryRole":
            applies = actor_id not in BYPASS_ROLE_IDS
        elif actor_type == "User":
            if bot_id is None:
                unresolved = True
                continue
            applies = actor_id == bot_id
        elif actor_type in BYPASS_ACTOR_TYPES_ABOVE_BOT:
            continue
        else:
            unresolved = True
            continue
        if not applies:
            continue
        mode = actor.get("bypass_mode")
        if mode in {"always", "exempt"}:
            applicable.append("always")
        elif mode == "pull_request":
            applicable.append("pull_requests_only")
        else:
            unresolved = True

    if "always" in applicable:
        return "always"
    if applicable and not unresolved:
        return "pull_requests_only"
    if unresolved:
        return None
    return "never"


def _ruleset_keeps_bot_out(data: dict, bot_name: str) -> bool | None:
    """Whether a fetched ruleset gives the bot no bypass at all."""
    bypass = _ruleset_bot_bypass(data, bot_name)
    return None if bypass is None else bypass == "never"


def _fetch_ruleset(repo: str, ruleset_id: int | str) -> dict | None:
    """A ruleset's detail, or None when unreadable.

    The repo-scoped endpoint serves organization- and enterprise-sourced
    rulesets too, so any applying ruleset can be fetched here.
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
    return data


def _ruleset_type_bypass(
    repo: str, branch: str, bot_name: str, rule_type: str
) -> str | None:
    """The effective bypass across all ``rule_type`` rulesets on a branch.

    Rulesets layer, so the most restrictive one wins: ``never`` blocks every
    update, then ``pull_requests_only``, then ``always``. ``absent`` means no
    rule of this type applies, which lets classic branch protection remain a
    distinct fallback. An unreadable ruleset makes the exact effective level
    unknown unless another readable ruleset already blocks all updates.
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

    matching_rules = [
        rule
        for rule in rules
        if isinstance(rule, dict) and rule.get("type") == rule_type
    ]
    if not matching_rules:
        return "absent"

    bypasses: list[str] = []
    unresolved = False
    for rule in matching_rules:
        ruleset_id = rule.get("ruleset_id")
        data = _fetch_ruleset(repo, ruleset_id) if ruleset_id is not None else None
        bypass = _ruleset_bot_bypass(data, bot_name) if data is not None else None
        if bypass == "never":
            return "never"
        if bypass is None:
            unresolved = True
        else:
            bypasses.append(bypass)
    if unresolved:
        return None
    if "pull_requests_only" in bypasses:
        return "pull_requests_only"
    return "always"


def update_ruleset_bypass(repo: str, branch: str, bot_name: str) -> str | None:
    """The effective bypass across all update rulesets on ``branch``."""
    return _ruleset_type_bypass(repo, branch, bot_name, "update")


def _tags_admin_gated(repo: str, bot_name: str) -> bool | None:
    """Whether an active all-tags ruleset keeps a write-access bot off every tag.

    True when a tag-target ruleset covers `~ALL` tags with nothing excluded,
    restricts `creation` and `update` (force-pushing an existing tag fires
    `update`), and its bypass configuration keeps the bot out
    (`_ruleset_keeps_bot_out`) — the shape install-tend's ref-protection step
    creates. Narrower patterns are not credited: deciding whether a pattern
    set covers an environment policy's tag entries would re-implement
    GitHub's matcher, and the recipe's rule is all-tags on purpose.
    """
    listed = _gh(
        "api",
        "--paginate",
        f"repos/{repo}/rulesets",
        "--jq",
        '.[] | select(.target == "tag" and .enforcement == "active") | .id',
    )
    if listed is None or listed.returncode != 0:
        return None

    unresolved = False
    for ruleset_id in listed.stdout.split():
        data = _fetch_ruleset(repo, ruleset_id)
        if data is None:
            unresolved = True
            continue
        ref_name = data.get("conditions", {}).get("ref_name", {})
        if ref_name.get("include") != ["~ALL"] or ref_name.get("exclude"):
            continue
        if not {"creation", "update"} <= {r.get("type") for r in data.get("rules", [])}:
            continue
        verdict = _ruleset_keeps_bot_out(data, bot_name)
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


def _lines(stdout: str) -> set[str]:
    """The non-empty lines of a `gh --jq` stream, which emits one result per line.

    Every secret listing below is read this way, and every one of them passes
    `--paginate`. Both halves are load-bearing: an Actions secrets endpoint
    serves 30 per page, and `gh api --paginate` applies `--jq` per page — so an
    array-building filter emits one array *per page* and their concatenation is
    not JSON. Reading a single page instead reports the tail as absent, which
    in `check_repo_secret_allowlist` reads as "no unexpected secret": the
    direction that hides exposure rather than announcing it.
    """
    return {line.strip() for line in stdout.splitlines() if line.strip()}


# The operational secrets live in a deployment-gated environment rather than at
# repo level, so every "is the secret set?" check reads them from there. A copy
# left at repo level defeats the gate entirely — any workflow can read it
# without naming the environment — and that is what `check_repo_secret_allowlist`
# now catches, since the operational names are no longer in its allowed set.
def _env_secret_names(repo: str) -> tuple[set[str] | None, str]:
    """Secret names in the tend environment. Returns (names, error message)."""
    result = _gh(
        "api",
        "--paginate",
        f"repos/{repo}/environments/{TEND_ENVIRONMENT}/secrets",
        "--jq",
        ".secrets[].name",
    )
    if result is None:
        return None, "gh CLI not found"
    if result.returncode != 0:
        return None, (
            f"Could not list secrets in the '{TEND_ENVIRONMENT}' environment "
            "(missing environment, or requires admin access). "
            "See the environment check above for how to create it."
        )
    return _lines(result.stdout), ""


def _env_path(env_name: str) -> str:
    """An environment name as one path segment.

    GitHub admits `/` in an environment name, and `gh api` treats the path it
    is given as already-formed — it percent-encodes a space but passes a slash
    through as a separator, so `a/b` addresses an environment that does not
    exist. Every such 404 reads to the callers below as a token without admin
    access, which returns the whole credential check as skipped. `safe=""`
    encodes the separator too; `gh` does not re-encode what it is handed.
    """
    return quote(env_name, safe="")


def _branch_policies(repo: str, env_name: str) -> list[dict] | None:
    """An environment's deployment branch policies, or None if unlistable.

    `--paginate`: a stale policy set is exactly the case that can exceed one
    page, and an unread tail is one a caller would treat as absent.
    """
    listed = _gh(
        "api",
        "--paginate",
        f"repos/{repo}/environments/{_env_path(env_name)}/deployment-branch-policies",
        "--jq",
        ".branch_policies[]",
    )
    if listed is None or listed.returncode != 0:
        return None
    try:
        return [json.loads(line) for line in listed.stdout.splitlines() if line]
    except json.JSONDecodeError:
        return None


def check_environment(repo: str, admitted: list[str]) -> CheckResult:
    """The Tend environment admits only verified operational refs.

    This is the whole mechanism: a job naming the environment runs only from a
    ref in its deployment branch policy, so a workflow pushed to a feature
    branch is refused before its first step. Under yolo, the default branch is
    admitted because its generated workflows are protected as control-plane
    code and run the agent inside Tend's credential-isolation sandbox.
    """
    name = "environment"
    if not admitted:
        # No branch was verified unwritable, so there is no ref the policy
        # could name. Whatever this environment says, the branch-protection
        # failure above is the thing to fix.
        return CheckResult(
            name,
            None,
            "No branch verified as protected, so the admitted set is unknown — "
            "fix branch protection first.",
        )
    result = _gh("api", f"repos/{repo}/environments/{TEND_ENVIRONMENT}")
    if result is None:
        return CheckResult(name, None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' not found. The operational "
            "secrets must live in it, gated to admin-only refs, or a workflow "
            "pushed to any branch can read them. Run `tend check --fix` to "
            f"create it admitting {', '.join(admitted)}, then move each secret "
            "into it and delete the repo-level copy.",
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
        # "Protected branches" keys on whether a rule covers the branch, not on
        # who may push it. Probed: under this mode a branch whose only
        # protection was `required_linear_history` — which blocks no push —
        # accepted a plain push and then read an environment secret, while an
        # unprotected branch was refused with zero steps. Only a named list is
        # verifiable from here.
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' admits all protected branches. "
            "Use a custom branch policy naming the default branch and any "
            "protected_branches, so the admitted set is the one tend verifies.",
        )

    policies = _branch_policies(repo, TEND_ENVIRONMENT)
    if policies is None:
        return CheckResult(name, None, "Could not list deployment branch policies")
    names = {p["name"] for p in policies}

    # The admitted set must match exactly, in both directions. An extra ref is
    # one tend does not verify the bot is kept off; a missing one refuses every
    # workflow triggered on it, which fails closed and so is invisible unless
    # the check that owns the setup says so.
    extra = names - set(admitted)
    if extra:
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' admits {', '.join(sorted(extra))}, "
            "which tend does not verify the bot is kept off. Restrict the policy "
            f"to: {', '.join(admitted)}.",
        )
    missing = set(admitted) - names
    if missing:
        return CheckResult(
            name,
            False,
            f"Environment '{TEND_ENVIRONMENT}' does not admit "
            f"{', '.join(sorted(missing))}, so every tend workflow triggered on "
            "those refs is refused before its first step. Run `tend check --fix`.",
        )
    return CheckResult(
        name,
        True,
        f"Environment '{TEND_ENVIRONMENT}' admits only {', '.join(sorted(names))}",
    )


def check_environment_deployments(repo: str) -> CheckResult:
    """No job files a GitHub deployment for the operational-secret environment.

    The environment is a secret scope rather than a deploy target, but GitHub
    files a deployment for every job that names one, against whatever the run
    belongs to — under `pull_request_target` that is the pull request itself,
    so a single omission puts a "<bot> deployed to <env>" line on every push
    to every PR. `deployment: false` is the only lever: the environment object
    takes `wait_timer`, `prevent_self_review`, `reviewers` and
    `deployment_branch_policy`, and nothing there suppresses the record.

    Generated workflows take the block from one macro that a generator test
    pins, so this is the same invariant for the workflows tend did not write —
    a repo's own hand-maintained jobs, where the omission is invisible to
    whoever makes it. The gate still holds and the secrets still arrive; the
    only symptom is noise in someone else's timeline, which is why nothing
    else catches it.
    """
    name = "environment-deployments"

    files = _fetch_workflow_files(repo)
    if files is None:
        return CheckResult(
            name, None, ".github/workflows could not be read from the default branch"
        )
    offenders = [
        f"{path} job '{job_id}'"
        for path, text in sorted(files.items())
        if text is not None
        for job_id in sorted(_parse_workflow(path, text).filed_deployments)
    ]
    if offenders:
        return CheckResult(
            name,
            False,
            f"Jobs name the '{TEND_ENVIRONMENT}' environment without "
            f"`deployment: false`, so GitHub files a deployment record for "
            f"every run and posts it on the pull request: {', '.join(offenders)}. "
            "Add `deployment: false` beside the environment's `name:` — a "
            "generated `tend-*.yaml` takes it from `uvx tend@latest init` "
            "instead of a hand edit.",
        )
    unread = sorted(path for path, text in files.items() if text is None)
    if unread:
        return CheckResult(
            name, None, f"Workflows could not be read: {', '.join(unread)}"
        )
    return CheckResult(
        name,
        True,
        f"No job files a deployment for the '{TEND_ENVIRONMENT}' environment",
    )


_WORKFLOWS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: "HEAD:.github/workflows") {
      ... on Tree {
        entries { name type object { ... on Blob { text } } }
      }
    }
  }
}
"""


def _fetch_workflow_files(repo: str) -> dict[str, str | None] | None:
    """Every workflow file on the repo's default branch, in one GraphQL call.

    Values are the file text, or None for a blob GitHub served without text
    (binary or oversized) — the caller reports those as unread rather than
    treating them as empty.

    Returns an empty dict when the repo has no `.github/workflows`, and None
    when the tree could not be read at all.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return None
    result = _gh(
        "api",
        "graphql",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-f",
        f"query={_WORKFLOWS_QUERY}",
    )
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    repository = (data.get("data") or {}).get("repository")
    if not isinstance(repository, dict):
        return None
    tree = repository.get("object")
    if tree is None:
        return {}
    files: dict[str, str | None] = {}
    for entry in tree.get("entries", []):
        entry_name = entry.get("name", "")
        if entry.get("type") != "blob" or not entry_name.endswith((".yml", ".yaml")):
            continue
        files[entry_name] = (entry.get("object") or {}).get("text")
    return files


@dataclass(frozen=True)
class _WorkflowFacts:
    """What one workflow file says about the repo's credential surface."""

    path: str
    steerable: frozenset[str] = frozenset()  # bot-steerable triggers it carries
    call_only: bool = False  # `workflow_call` is the only thing that starts it
    calls: frozenset[str] = frozenset()  # local reusable workflows it invokes
    # job ids whose reusable workflow is not the same-commit ``./`` form
    external_calls: frozenset[str] = frozenset()
    external_oidc: frozenset[str] = frozenset()  # …of those, ones granting OIDC
    environments: frozenset[str] = frozenset()  # environments its jobs deploy to
    oidc_environments: frozenset[str] = frozenset()  # …of those, ones minting OIDC
    oidc_without_environment: frozenset[str] = frozenset()  # job ids minting ungated
    filed_deployments: frozenset[str] = frozenset()  # job ids naming tend that file
    unresolved: tuple[str, ...] = ()


def _permissions_grant_oidc(permissions: object) -> bool:
    """Whether a `permissions:` block lets the job mint an OIDC token."""
    if isinstance(permissions, str):
        return permissions == "write-all"
    if isinstance(permissions, dict):
        return permissions.get("id-token") == "write"
    return False


def _called_workflow(uses: str) -> str | None:
    """The same-commit local workflow that a job-level ``uses:`` names.

    Only ``./.github/workflows/...`` is inspectable from the fetched tree. An
    ``owner/repo/...@ref`` call is unresolved even when owner/repo is this
    repository: the ref may name historical workflow code with different
    environment use.
    """
    relative = "./.github/workflows/"
    if uses.startswith(relative):
        return uses[len(relative) :]
    return None


def _parse_workflow(path: str, text: str) -> _WorkflowFacts:
    """Read one workflow's triggers, environments, and OIDC use.

    Anything the parse cannot decide (an unparsable file, an environment named
    by an expression) lands in `unresolved` rather than being silently dropped
    — a path tend cannot see is not a path tend can call gated.
    """
    unparsable = (f"{path} could not be parsed as a workflow",)
    try:
        data = YAML(typ="safe").load(io.StringIO(text))
    except (YAMLError, ValueError):
        return _WorkflowFacts(path, unresolved=unparsable)
    if not isinstance(data, dict):
        return _WorkflowFacts(path, unresolved=unparsable)

    # YAML 1.1 documents (`%YAML 1.1`) turn the `on:` key into the boolean
    # True; 1.2, which ruamel's safe loader defaults to, keeps it a string.
    on = data.get("on", data.get(True))
    if isinstance(on, str):
        triggers = {on}
    elif isinstance(on, (list, dict)):
        triggers = {t for t in on if isinstance(t, str)}
    else:
        triggers = set()

    steerable = triggers & BOT_STEERABLE_TRIGGERS
    dispatch = on.get("workflow_dispatch") if isinstance(on, dict) else None
    if isinstance(dispatch, dict) and dispatch.get("inputs"):
        steerable.add("workflow_dispatch")

    workflow_permissions = data.get("permissions")
    jobs = data.get("jobs")
    jobs = jobs if isinstance(jobs, dict) else {}

    calls: set[str] = set()
    external_calls: set[str] = set()
    external_oidc: set[str] = set()
    environments: set[str] = set()
    oidc_environments: set[str] = set()
    oidc_without_environment: set[str] = set()
    filed_deployments: set[str] = set()
    unresolved: list[str] = []

    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        permissions = job.get("permissions", workflow_permissions)
        oidc = _permissions_grant_oidc(permissions)

        uses = job.get("uses")
        if uses is not None:
            # A job that calls another workflow declares no environment of its
            # own — the called workflow's jobs do. Those are parsed here when
            # the call lands in this repo, and are unreadable when it doesn't,
            # which is what `external_calls` records. Its `permissions:` only
            # cap what the callee may request, so a callee mints OIDC on this
            # repo's behalf exactly when the calling job grants it.
            called = _called_workflow(uses) if isinstance(uses, str) else None
            if called is not None:
                calls.add(called)
            else:
                external_calls.add(str(job_id))
                if oidc:
                    external_oidc.add(str(job_id))
            continue

        declared = job.get("environment")
        environment = declared
        deployment = None
        if isinstance(declared, dict):
            environment = declared.get("name")
            deployment = declared.get("deployment")
        if environment is None:
            if oidc:
                oidc_without_environment.add(str(job_id))
            continue
        if not isinstance(environment, str) or "${{" in environment:
            unresolved.append(
                f"{path} job '{job_id}' names its environment dynamically"
            )
            continue
        # GitHub environment names are case-insensitive. Keep one canonical
        # representation everywhere facts are compared; retain the API's
        # spelling only where it is needed to address or display the object.
        environment = environment.casefold()
        environments.add(environment)
        # The operational-secret environment is a secret scope, so a job naming
        # it deploys nothing and the record GitHub would file for it is pure
        # noise on whatever the run belongs to. Only the shorthand and an
        # explicit `deployment: true` file one; both are the same mistake.
        if environment == TEND_ENVIRONMENT and deployment is not False:
            filed_deployments.add(str(job_id))
        if oidc:
            oidc_environments.add(environment)

    return _WorkflowFacts(
        path=path,
        steerable=frozenset(steerable),
        call_only=triggers == {"workflow_call"},
        calls=frozenset(calls),
        external_calls=frozenset(external_calls),
        external_oidc=frozenset(external_oidc),
        environments=frozenset(environments),
        oidc_environments=frozenset(oidc_environments),
        oidc_without_environment=frozenset(oidc_without_environment),
        filed_deployments=frozenset(filed_deployments),
        unresolved=tuple(unresolved),
    )


def _effective_triggers(
    facts: dict[str, _WorkflowFacts],
) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """Resolve each workflow's steerable triggers, following `workflow_call`.

    `workflow_call` on its own says only that a workflow is callable; what can
    start it is whatever starts its callers. Callers within the repo are
    followed to a fixpoint, and a workflow no such chain reaches is returned as
    unreached — the only thing left that can start it is a caller in another
    repo.

    A workflow carrying triggers of its own anchors the chain: `workflow_call`
    widens the way in rather than replacing it, so its own `on:` starts it here
    whatever else calls it. A callable workflow is then reached when one of its
    callers is, and unreached when every route to it runs through a workflow
    only an outside caller can start.

    An unreached workflow spends nothing of this repo's, a point GitHub's
    reusable-workflow docs leave unsaid: a run belongs to the repo that starts
    it, down into any workflow it calls elsewhere. The secrets, the environment
    a callee's `environment:` names, the deployment record and the OIDC `sub`
    are the caller's throughout, and the caller's gate on that environment is
    what holds the run. So the fact cuts both ways — a call arriving from
    outside reaches nothing here, and one leaving for outside carries this
    repo's environments with it.
    """
    resolved = {path: f.steerable for path, f in facts.items()}
    reached = {path: not f.call_only for path, f in facts.items()}
    callers: dict[str, set[str]] = {path: set() for path in facts}
    for path, f in facts.items():
        for callee in f.calls:
            if callee in callers:
                callers[callee].add(path)

    # Each pass only grows the trigger sets and only flips workflows to
    # reached, so this settles; the iteration bound keeps a cyclic `uses:`
    # graph from looping.
    for _ in range(len(facts) + 1):
        changed = False
        for path, sources in callers.items():
            if not sources:
                continue
            grown = resolved[path].union(*(resolved[s] for s in sources))
            if grown != resolved[path]:
                resolved[path] = grown
                changed = True
            if not reached[path] and any(reached[s] for s in sources):
                reached[path] = True
                changed = True
        if not changed:
            break

    unreached = frozenset(path for path, ok in reached.items() if not ok)
    return resolved, unreached


@dataclass(frozen=True)
class _CredentialSurface:
    """The repo's credential-spending surface, as read from its workflows."""

    env_steerable: dict[str, frozenset[str]]
    oidc_environments: frozenset[str]
    # (workflow path, job id) pairs minting OIDC outside any environment
    ungated_oidc: tuple[tuple[str, str], ...]
    unresolved: tuple[str, ...]


def _credential_surface(files: dict[str, str | None] | None) -> _CredentialSurface:
    """Read the workflows into the facts the environment gates need.

    An unreadable tree yields an empty surface that says so, rather than no
    surface at all: the environment gates below still verify, and only the
    parts that need the workflows report themselves unread.
    """
    if files is None:
        return _CredentialSurface(
            {},
            frozenset(),
            (),
            (".github/workflows could not be read from the default branch",),
        )

    facts: dict[str, _WorkflowFacts] = {}
    unresolved: list[str] = []
    for path, text in sorted(files.items()):
        if text is None:
            unresolved.append(f"{path} could not be read")
            continue
        facts[path] = _parse_workflow(path, text)

    resolved, unreached = _effective_triggers(facts)
    env_steerable: dict[str, set[str]] = {}
    oidc_environments: set[str] = set()
    ungated_oidc: list[tuple[str, str]] = []
    for path, f in facts.items():
        if path in unreached:
            # Nothing here starts it, and the outside caller that does spends
            # its own repo's credentials, not this one's — so neither what its
            # jobs name nor what it leaves unreadable is this repo's surface to
            # gate. A file that would not parse is never unreached: nothing
            # read `workflow_call` off it, so its own unknown still reports.
            continue
        unresolved.extend(f.unresolved)
        for env in f.environments:
            env_steerable.setdefault(env, set()).update(resolved[path])
        oidc_environments |= f.oidc_environments
        ungated_oidc.extend((path, job) for job in sorted(f.oidc_without_environment))
        # A call out of the repo runs against this repo's environments out of a
        # file that cannot be read from here, so what it deploys to is unknown.
        # That only matters where it could change a verdict: a steerable
        # trigger defeats any ref policy the environment is gated by, and a
        # granted `id-token: write` may be minted outside an environment
        # altogether, which is `ungated_oidc`'s finding when it is visible.
        if f.external_calls and resolved[path]:
            triggers = ", ".join(f"`{t}`" for t in sorted(resolved[path]))
            jobs = ", ".join(f"'{j}'" for j in sorted(f.external_calls))
            unresolved.append(
                f"{path} runs on {triggers} and calls a ref-qualified or "
                f"external workflow ({jobs}), whose environment use is not "
                "visible from this tree"
            )
        unresolved.extend(
            f"{path} job '{job}' grants `id-token: write` to another repo's "
            "workflow, so whether the token is minted inside an environment is "
            "not visible here"
            for job in sorted(f.external_oidc)
        )

    return _CredentialSurface(
        env_steerable={e: frozenset(t) for e, t in env_steerable.items()},
        oidc_environments=frozenset(oidc_environments),
        ungated_oidc=tuple(sorted(ungated_oidc)),
        unresolved=tuple(sorted(set(unresolved))),
    )


def check_yolo_workflows(repo: str, cfg: Config) -> CheckResult:
    """Require the default branch to contain only the audited generated jobs.

    The ``tend`` environment may release operational secrets on main in yolo,
    so its exception cannot extend to an adopter workflow or a stale generated
    workflow that still executes repository code as the runner.
    """
    name = "yolo-workflows"
    files = _fetch_workflow_files(repo)
    if files is None:
        return CheckResult(
            name, None, "Could not read workflows from the default branch"
        )

    expected = {workflow.filename: workflow.content for workflow in generate_all(cfg)}
    mismatched = sorted(
        filename
        for filename, content in expected.items()
        if files.get(filename) != content
    )
    extra_holders: list[str] = []
    unresolved: list[str] = []
    external_calls: list[str] = []
    for filename, content in sorted(files.items()):
        if filename in expected or content is None:
            continue
        facts = _parse_workflow(filename, content)
        if TEND_ENVIRONMENT.casefold() in facts.environments:
            extra_holders.append(filename)
        if facts.unresolved:
            unresolved.append(filename)
        if facts.external_calls:
            external_calls.append(filename)
    unread = sorted(filename for filename, content in files.items() if content is None)
    if mismatched or extra_holders or unresolved or external_calls:
        details = []
        if mismatched:
            details.append(f"not current generated output: {', '.join(mismatched)}")
        if extra_holders:
            details.append(
                "non-generated files use the tend environment: "
                f"{', '.join(extra_holders)}"
            )
        if unresolved:
            details.append(
                f"environment use could not be resolved in: {', '.join(unresolved)}"
            )
        if external_calls:
            details.append(
                "external or ref-qualified reusable workflows may use the "
                "tend environment: "
                f"{', '.join(external_calls)}"
            )
        return CheckResult(
            name,
            False,
            "Yolo requires Tend's operational secrets to reach only the audited "
            f"generated workflows ({'; '.join(details)}). Run `tend init`, "
            "merge its control-plane changes, and move any other job to a "
            "separate reviewer-gated environment.",
        )
    if unread:
        return CheckResult(
            name, None, f"Workflow files could not be read: {', '.join(unread)}"
        )
    return CheckResult(
        name,
        True,
        "The default branch has the exact generated yolo workflows and no other "
        "workflow uses the tend environment.",
    )


def _reviewer_gate(env: dict, bot_name: str) -> str | None:
    """Why this environment's reviewer gate does not hold, or None if it does.

    A Team reviewer is unresolvable from here for the same reason a Team bypass
    actor is (see BYPASS_ACTOR_TYPES_ABOVE_BOT): the bot may be a member, so any
    approval the team could give, the bot might be giving itself.
    """
    entries = [
        r
        for rule in env.get("protection_rules", [])
        if rule.get("type") == "required_reviewers"
        for r in rule.get("reviewers", [])
    ]
    if not entries:
        return "has no required reviewers"
    if any(r.get("type") == "Team" for r in entries):
        return (
            "requires approval from a team, whose membership is not visible here"
            f" — confirm '{bot_name}' is not in it, or name individual reviewers"
        )
    reviewers = [r["reviewer"]["login"] for r in entries]
    if any(_same_login(login, bot_name) for login in reviewers):
        return f"lists the bot ('{bot_name}') as a reviewer, so it approves its own run"
    return None


@dataclass(frozen=True)
class _Gap:
    """Why a gate does not hold, and whether that verdict was verified.

    `verified` is False when the token could not see enough to decide, which
    is not the same finding as a gate confirmed absent: the module docstring's
    invariant is that the nightly sees the answers a maintainer does, so where
    it doesn't, the honest report is unknown. `check_branch_protection` takes
    the same stance on an unreadable bypass list.
    """

    reason: str
    verified: bool = True


def _policy_gate(
    repo: str,
    env_name: str,
    env: dict,
    admitted: list[str],
    tags_ok,
    steerable: frozenset[str],
) -> _Gap | None:
    """Why this environment's deployment policy does not gate the bot, or None.

    A policy gates only when every entry names a ref verified out of the bot's
    reach: a branch in `admitted`, or tags under an admin-only all-tags
    ruleset (`tags_ok`, computed lazily since most repos have no tag entries).
    A pattern entry is refused rather than matched — deciding what a pattern
    covers would re-implement GitHub's matcher.

    A ref-gated policy still loses to a trigger the bot fires and steers
    itself (`steerable`), since the run starts from a ref the policy already
    admits. Only the reviewer gate covers those. A workflow carrying such a
    trigger counts even when an `if:` on the deploying job would skip that
    event — reading the expression to decide otherwise is the same
    re-implementation the pattern rule above declines, and the conservative
    answer fails closed.
    """
    policy = env.get("deployment_branch_policy")
    if not policy:
        return _Gap("has no deployment branch policy, so every ref reaches its secrets")
    if policy.get("protected_branches"):
        return _Gap(
            "admits all protected branches, which keys on a rule covering the "
            "branch, not on who may push it"
        )
    policies = _branch_policies(repo, env_name)
    if policies is None:
        return _Gap(
            "has a deployment branch policy this token cannot list", verified=False
        )
    # An unverifiable entry is held, not returned: a later entry can name a ref
    # confirmed out of the verified set, and that finding outranks this one —
    # the precedence `check_credential_environments` already applies when both
    # kinds arrive from different environments.
    unverified: _Gap | None = None
    for p in policies:
        if p.get("type") == "tag":
            gated = tags_ok()
            if gated is None:
                # Every unread input lands here: a failed ruleset read, a
                # withheld bypass list, and a bypass actor naming a principal
                # tend cannot resolve are all None. The message names the
                # set, folding in which identity settles each — the bot's
                # own run reads GitHub's verdict on it (all but failed
                # reads), an admin reads the list.
                unverified = _Gap(
                    "admits tags, and whether an all-tags ruleset gates them "
                    "is unverifiable with this token — a ruleset read "
                    "failed, a bypass list is withheld (a repo admin reads "
                    "one; the bot's own run reads GitHub's verdict on it "
                    "without needing the list), or a bypass actor names a "
                    "principal tend cannot resolve",
                    verified=False,
                )
            elif gated is False:
                return _Gap(
                    "admits tags, and no active all-tags ruleset restricting "
                    "creation and update to admins could be verified"
                )
        elif p["name"] not in admitted:
            return _Gap(
                f"admits '{p['name']}', which tend has not verified the bot "
                "cannot write"
            )
    if steerable:
        triggers = ", ".join(f"`{t}`" for t in sorted(steerable))
        # Not "admits only verified refs": a held `unverified` means one entry
        # didn't settle. The ref list is beside the point here anyway — the bot
        # picks the ref — so the message states the trigger, which holds either
        # way, and this stays a verified finding.
        return _Gap(
            f"is reached by a workflow running on {triggers}, which the bot "
            "fires and steers against a ref the policy already admits, so its "
            "ref list does not gate it"
        )
    return unverified


def check_credential_environments(
    repo: str, cfg: Config, admitted: list[str]
) -> CheckResult:
    """Every environment holding a credential is gated against the bot.

    A credential is released only to a job naming its environment, so the
    environment's own gate is the whole question — for release tokens exactly
    as for the operational secrets, which is what lets the security model
    claim a run the bot can cause reaches no credential at all. A gate is a
    required reviewer that is not the bot, or a deployment policy admitting
    only refs verified out of the bot's reach and carrying no trigger the bot
    can steer (`_policy_gate`); either suffices, since each alone stops the
    bot causing a run that the environment feeds. `tend` itself is
    `check_environment`'s job.

    An environment holds a credential when it stores a secret, or when a job
    deploying to it requests `id-token: write` — trusted publishing (PyPI,
    npm, a cloud role) stores no secret, and an environment sweep keyed on
    stored secrets alone walks straight past the repos that publish. Keyed on
    holding one rather than on any name, because a check that reads names
    passes when an environment is renamed or a new one is stood up beside it.

    `id-token: write` outside any environment is the ungated case of the same
    thing: the minted token carries no environment claim, and nothing gates
    the ref it comes from, so a trust policy pinning the repository but not
    the ref accepts one the bot minted from a branch it pushed.
    """
    name = "credential-environments"

    listed = _gh(
        "api",
        "--paginate",
        f"repos/{repo}/environments",
        "--jq",
        ".environments[].name",
    )
    if listed is None:
        return CheckResult(name, None, "gh CLI not found")
    if listed.returncode != 0:
        return CheckResult(
            name, None, f"Could not list environments: {listed.stderr.strip()}"
        )

    surface = _credential_surface(_fetch_workflow_files(repo))
    tags_ok = cache(lambda: _tags_admin_gated(repo, cfg.bot_name))

    ungated: list[str] = []
    unverified: list[str] = []
    holders: list[str] = []
    # One name per line, not one per whitespace-separated token: GitHub admits
    # a space in an environment name, and splitting on whitespace turns one
    # such environment into two names that exist nowhere. Each answers 404,
    # which is the `returncode != 0` below, so the whole check reports itself
    # skipped for want of admin access — a credential check that stops
    # verifying and blames the token. The real environment goes unexamined
    # either way.
    for env_name in listed.stdout.splitlines():
        if not env_name:
            continue
        normalized_env = env_name.casefold()
        secrets = _gh(
            "api",
            "--paginate",
            f"repos/{repo}/environments/{_env_path(env_name)}/secrets",
            "--jq",
            ".secrets[].name",
        )
        if secrets is None or secrets.returncode != 0:
            return CheckResult(
                name,
                None,
                f"Could not list secrets in '{env_name}' (requires admin access)",
            )
        if (
            not secrets.stdout.split()
            and normalized_env not in surface.oidc_environments
        ):
            continue
        holders.append(env_name)
        if normalized_env == TEND_ENVIRONMENT.casefold():
            continue  # Gated by its branch policy; `environment` verifies that.
        detail = _gh("api", f"repos/{repo}/environments/{_env_path(env_name)}")
        if detail is None or detail.returncode != 0:
            return CheckResult(name, None, f"Could not read environment '{env_name}'")
        try:
            env = json.loads(detail.stdout)
        except json.JSONDecodeError:
            return CheckResult(name, None, f"Could not parse environment '{env_name}'")
        reviewer_reason = _reviewer_gate(env, cfg.bot_name)
        if reviewer_reason is None:
            continue
        gap = _policy_gate(
            repo,
            env_name,
            env,
            admitted,
            tags_ok,
            surface.env_steerable.get(normalized_env, frozenset()),
        )
        if gap is None:
            continue
        found = f"'{env_name}' {reviewer_reason}, and {gap.reason}"
        (ungated if gap.verified else unverified).append(found)

    if surface.ungated_oidc:
        jobs = ", ".join(f"{path}:{job}" for path, job in surface.ungated_oidc)
        ungated.append(
            f"{len(surface.ungated_oidc)} job(s) request `id-token: write` outside "
            "any environment, so nothing gates the ref the token is minted from "
            f"({jobs})"
        )

    if ungated:
        return CheckResult(
            name,
            False,
            "A run the bot can cause reaches a credential: "
            f"{'; '.join(ungated)}. Gate each environment with a required "
            "reviewer that is not the bot, or a deployment policy listing "
            "only verified refs — branches this run confirmed the bot cannot "
            "write, or tags under an admin-only all-tags ruleset; move an "
            "OIDC job into such an environment. The policy's 'protected "
            "branches' setting is not one of them.",
        )
    if unverified or surface.unresolved:
        return CheckResult(
            name,
            None,
            "Could not read the whole credential surface: "
            f"{'; '.join([*unverified, *surface.unresolved])}",
        )
    if not holders:
        return CheckResult(name, True, "No environment holds a credential")
    return CheckResult(
        name, True, f"Credential-holding environments are gated: {', '.join(holders)}"
    )


def check_secrets(repo: str, expected: list[str]) -> CheckResult:
    """Check that required secrets exist in the environment.

    An org-level copy is a failure here, not a stand-in: the environment
    cannot gate an org secret, so any workflow the bot pushes reads it.
    `check_repo_secret_allowlist` flags the same copy (best-effort), and a
    pass on availability would sit beside that failure calling the same
    secret fine — while every workflow keeps working, which is why the
    failure names where the working copy lives.
    """
    secret_names, err = _env_secret_names(repo)
    if secret_names is None:
        return CheckResult("secrets", None, err)

    missing = [s for s in expected if s not in secret_names]
    if not missing:
        return CheckResult(
            "secrets", True, f"Required secrets present: {', '.join(expected)}"
        )

    org = repo.split("/")[0] if "/" in repo else None
    org_secrets, org_forbidden = _list_org_secrets(org, repo) if org else (None, False)
    found_at_org = [s for s in missing if org_secrets and s in org_secrets]

    msg = (
        f"Missing from the '{TEND_ENVIRONMENT}' environment: {', '.join(missing)}. "
        f"Add each with `gh secret set <NAME> --repo {repo} --env {TEND_ENVIRONMENT}` — "
        "a repo-level copy is readable by any workflow the bot pushes."
    )
    if found_at_org:
        msg += (
            f"\n{', '.join(found_at_org)} exists at org level, so everything "
            "keeps working — ungated, since the environment cannot cover an "
            "org secret. Remove the org copy or unshare it from this repo."
        )
    if org_forbidden:
        msg += (
            "\nNote: Could not check for an org-level copy (HTTP 403), which "
            "would keep workflows running ungated. Grant the admin:org scope "
            "to check: gh auth refresh -h github.com -s admin:org"
        )
    return CheckResult("secrets", False, msg)


def check_memory_gist_repository(repo: str) -> CheckResult:
    """Secret Gists are unlisted rather than private, so memory is public-only."""
    is_public = _repo_is_public(repo)
    if is_public is None:
        return CheckResult(
            "memory-gist",
            None,
            f"Could not determine whether {repo} is public",
        )
    if not is_public:
        return CheckResult(
            "memory-gist",
            False,
            "memory_gist is experimental and available only for public repositories: "
            "a secret Gist is unlisted, not private",
        )
    return CheckResult(
        "memory-gist",
        True,
        "memory_gist is experimental and limited to this public repository",
    )


def _repo_is_public(repo: str) -> bool | None:
    """Whether `repo` is public. None when it cannot be determined."""
    result = _gh("api", f"repos/{repo}", "--jq", ".private")
    if result is None or result.returncode != 0:
        return None
    return {"true": False, "false": True}.get(result.stdout.strip())


def _org_secret_repos(org: str, name: str) -> set[str] | None:
    """Repos a `selected`-visibility org secret is shared with, or None if the
    list cannot be read. Paginated: an org sharing a secret with more repos
    than one page holds would otherwise look like it omits this one."""
    result = _gh(
        "api",
        "--paginate",
        f"orgs/{org}/actions/secrets/{name}/repositories",
        "--jq",
        ".repositories[].full_name",
    )
    if result is None or result.returncode != 0:
        return None
    return _lines(result.stdout)


def _org_plan_is_free(org: str) -> bool:
    """Whether the org is on GitHub Free, which serves org secrets to public
    repositories only. False whenever the plan can't be read — the caller
    skips secrets on this, and skipping wrongly would blind the check."""
    result = _gh("api", f"orgs/{org}", "--jq", '.plan.name // ""')
    if result is None or result.returncode != 0:
        return False
    return result.stdout.strip() == "free"


def _list_org_secrets(org: str, repo: str) -> tuple[set[str] | None, bool]:
    """List the org-level secrets `repo` can actually read.

    Returns (secrets, permission_denied). An org secret scoped away from this
    repo is not part of its credential surface, and naming it produces a
    failure no repo-side change can clear: the repo is already at the tightest
    scoping GitHub offers, so the only lever left is a `secrets.allowed` entry
    that would assert the opposite of the truth and mute the name permanently.

    Filtering is fail-safe in one direction only — a secret whose reach cannot
    be determined stays in the set. Under-reporting hides real exposure;
    over-reporting is merely noise.
    """
    result = _gh(
        "api",
        "--paginate",
        f"orgs/{org}/actions/secrets",
        "--jq",
        ".secrets[] | {name, visibility}",
    )
    if result is None:
        return None, False
    if result.returncode != 0:
        forbidden = "HTTP 403" in result.stderr
        return None, forbidden
    try:
        listed = [
            (s["name"], s.get("visibility"))
            for s in (json.loads(line) for line in _lines(result.stdout))
        ]
    except (json.JSONDecodeError, TypeError, KeyError):
        return None, False

    is_public = _repo_is_public(repo)
    if is_public is False and _org_plan_is_free(org):
        return set(), False

    reachable = set()
    for name, visibility in listed:
        if visibility == "selected":
            shared = _org_secret_repos(org, name)
            if shared is not None and repo.casefold() not in {
                r.casefold() for r in shared
            }:
                continue
        elif visibility == "private" and is_public:
            continue
        reachable.add(name)
    return reachable, False


def check_repo_secret_allowlist(repo: str, allowed: set[str]) -> CheckResult:
    """Check that secrets available to workflows are in the allowlist.

    Checks repo-level secrets (always) and org-level secrets (best-effort).
    Any secret not in the allowlist is flagged — this catches release secrets
    (registry tokens, signing keys) that should be in a protected GitHub
    Environment instead.
    """
    result = _gh(
        "api", "--paginate", f"repos/{repo}/actions/secrets", "--jq", ".secrets[].name"
    )
    if result is None:
        return CheckResult("repo-secret-allowlist", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            "repo-secret-allowlist",
            None,
            "Could not list secrets (may require admin access)",
        )

    repo_secrets = _lines(result.stdout)

    # Best-effort: include the org-level secrets this repo can read (also
    # available to its workflows). Ones scoped away from it are not.
    org = repo.split("/")[0] if "/" in repo else None
    org_secrets: set[str] = set()
    org_forbidden = False
    if org:
        fetched, org_forbidden = _list_org_secrets(org, repo)
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


def _restrict_updates_ruleset(
    extra_branches: list[str],
    *,
    name: str = "Merge access",
    include_default: bool = True,
    bot_id: int | None = None,
    bot_bypass_mode: str | None = None,
) -> str:
    """Build one canonical branch update ruleset."""
    include = (["~DEFAULT_BRANCH"] if include_default else []) + [
        f"refs/heads/{branch}" for branch in extra_branches
    ]
    bypass_actors = [
        {
            "actor_id": ROLE_ID_ADMIN,
            "actor_type": "RepositoryRole",
            "bypass_mode": "exempt",
        }
    ]
    if bot_id is not None and bot_bypass_mode is not None:
        bypass_actors.append(
            {
                "actor_id": bot_id,
                "actor_type": "User",
                "bypass_mode": bot_bypass_mode,
            }
        )
    return json.dumps(
        {
            "name": name,
            "target": "branch",
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": include,
                    "exclude": [],
                }
            },
            "rules": [
                {"type": "creation"},
                {"type": "update"},
                {"type": "deletion"},
            ],
            "bypass_actors": bypass_actors,
        }
    )


def _control_plane_ruleset() -> str:
    """Build the CODEOWNERS-backed review rule used by yolo merge mode."""
    return json.dumps(
        {
            "name": "Control-plane review",
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "allowed_merge_methods": ["merge", "squash", "rebase"],
                        "dismiss_stale_reviews_on_push": True,
                        "require_code_owner_review": True,
                        "require_last_push_approval": False,
                        "required_approving_review_count": 0,
                        "required_review_thread_resolution": False,
                    },
                }
            ],
            "bypass_actors": [
                {
                    "actor_id": ROLE_ID_ADMIN,
                    "actor_type": "RepositoryRole",
                    "bypass_mode": "exempt",
                }
            ],
        }
    )


def _tag_operations_ruleset() -> str:
    """Build the canonical admin-gated all-tags ruleset body."""
    return json.dumps(
        {
            "name": "Tag operations",
            "target": "tag",
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": ["~ALL"],
                    "exclude": [],
                }
            },
            "rules": [{"type": "creation"}, {"type": "update"}],
            "bypass_actors": [
                {
                    "actor_id": ROLE_ID_ADMIN,
                    "actor_type": "RepositoryRole",
                    "bypass_mode": "exempt",
                }
            ],
        }
    )


def operational_refs(results: list[CheckResult]) -> list[str]:
    """Branches verified for Tend's own generated, sandboxed workflows."""
    prefix = "branch-protection:"
    return list(
        dict.fromkeys(
            r.name[len(prefix) :]
            for r in results
            if r.name.startswith(prefix) and r.passed is True
        )
    )


def credential_safe_refs(
    results: list[CheckResult], cfg: Config, default_branch: str
) -> list[str]:
    """Refs that may gate credentials outside Tend's hardened runtime.

    In yolo mode the bot can cause arbitrary ordinary code to land on the
    default branch, so a generic credential environment may admit that branch
    only when it also requires a non-bot reviewer. Extra protected branches stay
    bot-inaccessible and remain safe ref gates.
    """
    refs = operational_refs(results)
    if cfg.merge_policy.bot_can_merge:
        return [branch for branch in refs if branch != default_branch]
    return refs


def fix_environment(repo: str, admitted: list[str]) -> CheckResult:
    """Create the tend environment and set its branch policy to `admitted`.

    PUT is create-or-update, so one call owns every environment failure:
    missing, no policy, protected-branches mode. The reconcile below then
    adds missing admitted refs and deletes extras. Secrets are not moved —
    their values cannot be read back, so minting them into the environment
    stays with the installer.
    """
    name = "environment"
    result = _gh(
        "api",
        "-X",
        "PUT",
        f"repos/{repo}/environments/{TEND_ENVIRONMENT}",
        "--input",
        "-",
        input=json.dumps(
            {
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                }
            }
        ),
    )
    if result is None:
        return CheckResult(name, None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            name, False, f"Failed to create environment: {result.stderr.strip()}"
        )

    policies = _branch_policies(repo, TEND_ENVIRONMENT)
    if policies is None:
        return CheckResult(name, None, "Could not list deployment branch policies")
    existing = {p["name"]: p["id"] for p in policies}

    for branch in admitted:
        if branch in existing:
            continue
        created = _gh(
            "api",
            "-X",
            "POST",
            f"repos/{repo}/environments/{TEND_ENVIRONMENT}/deployment-branch-policies",
            "-f",
            f"name={branch}",
            "-f",
            "type=branch",
        )
        if created is None or created.returncode != 0:
            stderr = created.stderr.strip() if created else "gh CLI not found"
            return CheckResult(name, False, f"Failed to admit {branch}: {stderr}")
    for branch, policy_id in existing.items():
        if branch in admitted:
            continue
        deleted = _gh(
            "api",
            "-X",
            "DELETE",
            f"repos/{repo}/environments/{TEND_ENVIRONMENT}"
            f"/deployment-branch-policies/{policy_id}",
        )
        if deleted is None or deleted.returncode != 0:
            stderr = deleted.stderr.strip() if deleted else "gh CLI not found"
            return CheckResult(name, False, f"Failed to remove {branch}: {stderr}")

    return CheckResult(
        name,
        True,
        f"Environment '{TEND_ENVIRONMENT}' admits only {', '.join(admitted)}. "
        "Move each operational secret into it and delete the repo-level copy.",
    )


def fix_immutable_releases(repo: str) -> CheckResult:
    """Enable GitHub's repository-level immutable releases setting."""
    result = _gh(
        "api",
        "-X",
        "PUT",
        "-H",
        f"X-GitHub-Api-Version: {IMMUTABLE_RELEASES_API_VERSION}",
        f"repos/{repo}/immutable-releases",
    )
    if result is None:
        return CheckResult("immutable-releases", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            "immutable-releases",
            False,
            f"Could not enable immutable releases: {result.stderr.strip()}",
        )
    return CheckResult(
        "immutable-releases",
        True,
        "Enabled immutable releases for future published releases.",
    )


def fix_tag_protection(repo: str) -> CheckResult:
    """Create the canonical admin-gated all-tags ruleset."""
    result = _gh(
        "api",
        f"repos/{repo}/rulesets",
        "--method",
        "POST",
        "--input",
        "-",
        input=_tag_operations_ruleset(),
    )
    if result is None:
        return CheckResult("tag-protection", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            "tag-protection",
            False,
            f"Failed to create tag ruleset: {result.stderr.strip()}",
        )
    return CheckResult(
        "tag-protection",
        True,
        "Created 'Tag operations' ruleset — only admins can create or update tags.",
    )


def _repository_rulesets(repo: str) -> dict[str, int] | None:
    """Repository rulesets by name, or None when they cannot be listed."""
    result = _gh(
        "api",
        "--paginate",
        f"repos/{repo}/rulesets",
        "--jq",
        '.[] | select(.source_type == "Repository") | [.id, .name] | @tsv',
    )
    if result is None or result.returncode != 0:
        return None
    rulesets: dict[str, int] = {}
    for line in result.stdout.splitlines():
        try:
            raw_id, name = line.split("\t", 1)
            ruleset_id = int(raw_id)
        except ValueError:
            return None
        if name in rulesets:
            return None
        rulesets[name] = ruleset_id
    return rulesets


def _reconcile_ruleset(
    repo: str, existing: dict[str, int], name: str, body: str
) -> str | None:
    """Create or update one named ruleset; return an error message on failure."""
    ruleset_id = existing.get(name)
    method = "POST" if ruleset_id is None else "PUT"
    endpoint = f"repos/{repo}/rulesets"
    if ruleset_id is not None:
        endpoint += f"/{ruleset_id}"
    result = _gh("api", endpoint, "--method", method, "--input", "-", input=body)
    if result is None:
        return "gh CLI not found"
    if result.returncode != 0:
        return result.stderr.strip()
    return None


def _remove_ruleset(repo: str, existing: dict[str, int], name: str) -> str | None:
    """Remove a named ruleset when it exists; return an error on failure."""
    ruleset_id = existing.get(name)
    if ruleset_id is None:
        return None
    result = _gh("api", "-X", "DELETE", f"repos/{repo}/rulesets/{ruleset_id}")
    if result is None:
        return "gh CLI not found"
    if result.returncode != 0:
        return result.stderr.strip()
    return None


def fix_branch_protection(
    repo: str,
    default_branch: str,
    bot_name: str,
    merge: str,
    extra_branches: list[str] | None = None,
) -> CheckResult:
    """Reconcile the merge and extra-branch rulesets without a protection gap."""
    name = f"branch-protection:{default_branch}"
    existing = _repository_rulesets(repo)
    if existing is None:
        return CheckResult(name, None, "Could not list repository rulesets")

    extra = [b for b in (extra_branches or []) if b != default_branch]
    protected_body = _restrict_updates_ruleset(
        extra,
        name="Protected branch access",
        include_default=False,
    )
    if extra:
        error = _reconcile_ruleset(
            repo, existing, "Protected branch access", protected_body
        )
    else:
        error = _remove_ruleset(repo, existing, "Protected branch access")
    if error:
        return CheckResult(
            name,
            False,
            f"Failed to reconcile protected branches: {error}",
        )

    bot_id = None
    bypass_mode = None
    if merge == "yolo":
        bot_id = _user_id(bot_name)
        if bot_id is None:
            return CheckResult(name, False, f"Could not resolve bot '{bot_name}'")
        bypass_mode = "pull_request"
        error = _reconcile_ruleset(
            repo, existing, "Control-plane review", _control_plane_ruleset()
        )
        if error:
            return CheckResult(
                name, False, f"Failed to reconcile control-plane review: {error}"
            )
        verified = check_control_plane_ruleset(repo, default_branch, bot_name)
        if verified.passed is not True:
            return CheckResult(
                name,
                verified.passed,
                "Control-plane review was written but did not verify; merge "
                f"access remains maintainer-only: {verified.message}",
            )

    merge_body = _restrict_updates_ruleset(
        [],
        bot_id=bot_id,
        bot_bypass_mode=bypass_mode,
    )
    error = _reconcile_ruleset(repo, existing, "Merge access", merge_body)
    if error:
        return CheckResult(name, False, f"Failed to reconcile merge access: {error}")
    if merge == "maintainer":
        error = _remove_ruleset(repo, existing, "Control-plane review")
        if error:
            return CheckResult(
                name, False, f"Failed to remove yolo control-plane review: {error}"
            )

    access = (
        "the bot may merge pull requests but cannot push directly"
        if merge == "yolo"
        else "only admins can update the default branch"
    )
    return CheckResult(
        name,
        True,
        f"Reconciled branch rulesets: {access}; extra protected branches remain "
        "admin-only.",
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
    required_secrets = [BOT_TOKEN_SECRET]
    if cfg.memory_gist:
        required_secrets.append(MEMORY_GIST_SECRET)

    # The operational secrets are deliberately absent from `allowed`: they
    # belong to the environment, and a copy left at repo level is readable by
    # any workflow the bot can push, which is exactly the hole the environment
    # closes. The allowlist check therefore flags them as unexpected.
    allowed = set(cfg.allowed_repo_secrets)

    results = [
        check_branch_protection(
            repo,
            default_branch,
            cfg.bot_name,
            expected_bypass=cfg.merge_policy.expected_runtime_bypass,
        )
    ]
    for branch in cfg.protected_branches:
        if branch != default_branch:
            results.append(check_branch_protection(repo, branch, cfg.bot_name))
    if cfg.merge_policy.requires_control_plane_review:
        generation_cfg = replace(
            cfg,
            default_branch=default_branch,
            repo_owner=detect_canonical_owner(repo) or "",
        )
        results.append(
            check_control_plane_codeowners(
                repo, default_branch, cfg.control_plane_owner, cfg.bot_name
            )
        )
        results.append(check_control_plane_ruleset(repo, default_branch, cfg.bot_name))
        results.append(check_yolo_workflows(repo, generation_cfg))
    results.append(check_bot_permission(repo, cfg.bot_name))
    results.append(check_tag_protection(repo, cfg.bot_name))
    results.append(check_immutable_releases(repo))
    operational = operational_refs(results)
    results.append(check_environment(repo, operational))
    results.append(check_environment_deployments(repo))
    results.append(
        check_credential_environments(
            repo, cfg, credential_safe_refs(results, cfg, default_branch)
        )
    )
    results.append(check_secrets(repo, required_secrets))
    if cfg.memory_gist:
        results.append(check_memory_gist_repository(repo))
    enabled_harnesses = cfg.enabled_harnesses() or {cfg.harness}
    if "claude" in enabled_harnesses:
        results.append(check_claude_auth(repo))
    if "codex" in enabled_harnesses:
        results.append(check_codex_auth(repo))
    results.append(check_repo_secret_allowlist(repo, allowed))
    return results


def check_claude_auth(repo: str) -> CheckResult:
    """Claude needs either CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY —
    both being absent is the failure mode. Both being set is fine; the
    action prefers the OAuth token.
    """
    names, err = _env_secret_names(repo)
    if names is None:
        return CheckResult("claude-auth", None, err)
    which = [s for s in (CLAUDE_TOKEN_SECRET, ANTHROPIC_API_KEY_SECRET) if s in names]
    if which:
        return CheckResult(
            "claude-auth", True, f"Claude auth secret present: {', '.join(which)}"
        )
    return CheckResult(
        "claude-auth",
        False,
        f"Claude harness selected but neither {CLAUDE_TOKEN_SECRET} nor "
        f"{ANTHROPIC_API_KEY_SECRET} is set in the '{TEND_ENVIRONMENT}' environment.",
    )


def check_codex_auth(repo: str) -> CheckResult:
    """Codex needs an API key or the complete subscription secret set."""
    names, err = _env_secret_names(repo)
    if names is None:
        return CheckResult("codex-auth", None, err)
    subscription = {
        CODEX_AUTH_SECRET,
        CODEX_REFRESH_AUTH_SECRET,
        CODEX_REFRESH_PAT_SECRET,
    }
    configured = subscription & names
    if configured == subscription:
        return CheckResult(
            "codex-auth",
            True,
            "Codex subscription auth secrets present: "
            f"{', '.join(sorted(subscription))}",
        )
    if configured:
        missing = subscription - names
        return CheckResult(
            "codex-auth",
            False,
            "Codex subscription auth is partially configured; missing from "
            f"the '{TEND_ENVIRONMENT}' environment: {', '.join(sorted(missing))}.",
        )
    if OPENAI_KEY_SECRET in names:
        return CheckResult(
            "codex-auth", True, f"Codex auth secret present: {OPENAI_KEY_SECRET}"
        )
    return CheckResult(
        "codex-auth",
        False,
        f"Codex harness selected but neither {OPENAI_KEY_SECRET} nor the "
        f"subscription set ({', '.join(sorted(subscription))}) is configured "
        f"in the '{TEND_ENVIRONMENT}' environment.",
    )
