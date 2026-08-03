"""Security checks for tend setup.

Verifies the repository has the security prerequisites described in
docs/security-model.md: branch protection on configured branches, bot
permission level, required secrets, and the release chain (environment
ref policies plus the tag ruleset that backs them).

Uses the `gh` CLI for GitHub API access. Checks degrade gracefully when
gh is unavailable or the token lacks permission. Everything read here is
readable with the bot's own write-scoped token, so the nightly run sees
the same answers a maintainer does.
"""

from __future__ import annotations

import fnmatch
import io
import json
import shutil
import subprocess
from dataclasses import dataclass

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from tend.config import Config


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

# Triggers a write-scoped actor can both fire *and* steer — it decides not
# only that the run happens but what the run publishes. A ref policy does not
# gate these, because the actor fires them at a ref the policy already allows;
# only a required reviewer does. Verified against live GitHub with a
# write-access (non-admin, non-bypass) collaborator:
#
#   - `release`: creating a release against an *existing* tag takes no tag
#     operation, so a tag ruleset does not stop it — and the release's body
#     and uploaded assets are the actor's own.
#   - `repository_dispatch`: the actor supplies `client_payload` wholesale.
#   - `workflow_dispatch` *with inputs* (added per workflow, not listed here):
#     the actor supplies the inputs.
#
# A `workflow_dispatch` with no inputs is deliberately absent, as are `push`,
# `create`, `pull_request`, `workflow_run`, `deployment` and `schedule`: each
# runs code fixed by the ref, so against an admin-gated ref the worst the
# actor achieves is re-publishing what an admin already published.
BOT_STEERABLE_TRIGGERS = frozenset({"release", "repository_dispatch"})

# Ruleset ref patterns that cover every tag in the repository. `~ALL` is
# GitHub's own name for it; the glob forms are what an adopter may have
# typed instead.
ALL_TAGS_PATTERNS = frozenset({"~ALL", "**", "refs/tags/*", "refs/tags/**"})


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


def _gh_json_lines(path: str, jq: str) -> list | None:
    """List every page of a `gh api` endpoint, one jq result per line.

    `--paginate` with `--jq` emits each page's results in turn, so a repo with
    more environments, rulesets, or secrets than fit on one page is read whole
    rather than silently cut off at the first 30. `jq` must yield objects —
    gh prints a bare string unquoted, which is not parseable JSON.

    Returns None when the call fails, and an empty list when it returns
    nothing.
    """
    result = _gh("api", "--paginate", path, "--jq", jq)
    if result is None or result.returncode != 0:
        return None
    try:
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except (json.JSONDecodeError, ValueError):
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
    return _bypass_list_blocks_bot(_ruleset_detail(repo, ruleset_id), bot_name)


def _ruleset_detail(repo: str, ruleset_id: int) -> dict | None:
    """One ruleset with its rules, conditions, and bypass list."""
    result = _gh("api", f"repos/{repo}/rulesets/{ruleset_id}")
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _bypass_list_blocks_bot(ruleset: dict | None, bot_name: str) -> bool | None:
    """Whether a fetched ruleset's `bypass_actors` keeps a write-access bot out.

    See `_ruleset_blocks_bot` for the tri-state contract.
    """
    if ruleset is None:
        return None
    actors = ruleset.get("bypass_actors")
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
            "in .config/tend.yaml. See docs/security-model.md.",
        )

    msg = "All secrets available to workflows are in allowlist"
    if org_forbidden:
        msg += " (could not check org-level — grant admin:org scope to verify)"
    return CheckResult("repo-secret-allowlist", True, msg)


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
    """What one workflow file says about the repo's release surface."""

    path: str
    steerable: frozenset[str]  # bot-steerable triggers it carries
    reusable: bool  # declares `workflow_call`
    calls: frozenset[str]  # local reusable workflows this one invokes
    environments: frozenset[str]  # environments its jobs deploy to
    oidc_environments: frozenset[str]  # …of those, ones a job mints OIDC in
    oidc_without_environment: frozenset[str]  # job ids minting OIDC ungated
    unresolved: tuple[str, ...]


def _permissions_grant_oidc(permissions: object) -> bool:
    """Whether a `permissions:` block lets the job mint an OIDC token."""
    if isinstance(permissions, str):
        return permissions == "write-all"
    if isinstance(permissions, dict):
        return permissions.get("id-token") == "write"
    return False


def _parse_workflow(path: str, text: str) -> _WorkflowFacts:
    """Read one workflow's triggers, environments, and OIDC use.

    Anything the parse cannot decide (an unparsable file, an environment
    named by an expression) lands in `unresolved` rather than being silently
    dropped — a release path tend cannot see is not a release path tend can
    call safe.
    """
    unparsable = (f"{path} could not be parsed as a workflow",)
    empty = frozenset[str]()
    try:
        data = YAML(typ="safe").load(io.StringIO(text))
    except (YAMLError, ValueError):
        return _WorkflowFacts(
            path, empty, False, empty, empty, empty, empty, unparsable
        )
    if not isinstance(data, dict):
        return _WorkflowFacts(
            path, empty, False, empty, empty, empty, empty, unparsable
        )

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
    environments: set[str] = set()
    oidc_environments: set[str] = set()
    oidc_without_environment: set[str] = set()
    unresolved: list[str] = []

    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if uses is not None:
            # A job that calls another workflow declares no environment of its
            # own — the called workflow's jobs do, and those are parsed there.
            # Its `permissions:` only caps what the callee may request.
            if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
                calls.add(uses.split("/")[-1])
            continue
        permissions = job.get("permissions", workflow_permissions)
        oidc = _permissions_grant_oidc(permissions)

        environment = job.get("environment")
        if isinstance(environment, dict):
            environment = environment.get("name")
        if environment is None:
            if oidc:
                oidc_without_environment.add(str(job_id))
            continue
        if not isinstance(environment, str) or "${{" in environment:
            unresolved.append(
                f"{path} job '{job_id}' names its environment dynamically"
            )
            continue
        environments.add(environment)
        if oidc:
            oidc_environments.add(environment)

    return _WorkflowFacts(
        path=path,
        steerable=frozenset(steerable),
        reusable="workflow_call" in triggers,
        calls=frozenset(calls),
        environments=frozenset(environments),
        oidc_environments=frozenset(oidc_environments),
        oidc_without_environment=frozenset(oidc_without_environment),
        unresolved=tuple(unresolved),
    )


def _effective_triggers(
    facts: dict[str, _WorkflowFacts],
) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """Resolve each workflow's steerable triggers, following `workflow_call`.

    A reusable workflow's own `on:` says only that it is callable; what can
    start it is whatever starts its callers. Callers within the repo are
    followed to a fixpoint. A reusable workflow with no caller here is
    returned as unreached — its callers may live in another repo, which this
    cannot enumerate.
    """
    resolved = {path: f.steerable for path, f in facts.items()}
    callers: dict[str, set[str]] = {path: set() for path in facts}
    for path, f in facts.items():
        for callee in f.calls:
            if callee in callers:
                callers[callee].add(path)

    # Each pass only adds triggers and the vocabulary is finite, so this
    # settles; the iteration bound keeps a cyclic `uses:` graph from looping.
    for _ in range(len(facts) + 1):
        changed = False
        for path, sources in callers.items():
            grown = (
                resolved[path].union(*(resolved[s] for s in sources))
                if sources
                else resolved[path]
            )
            if grown != resolved[path]:
                resolved[path] = grown
                changed = True
        if not changed:
            break

    unreached = frozenset(
        path for path, f in facts.items() if f.reusable and not callers[path]
    )
    return resolved, unreached


@dataclass(frozen=True)
class _ReleaseSurface:
    """The repo's publish-capable surface, as read from its workflows."""

    env_steerable: dict[str, frozenset[str]]
    oidc_environments: frozenset[str]
    # (workflow path, job id) pairs minting OIDC outside any environment
    ungated_oidc: tuple[tuple[str, str], ...]
    unresolved: tuple[str, ...]


def _release_surface(files: dict[str, str | None]) -> _ReleaseSurface:
    facts: dict[str, _WorkflowFacts] = {}
    unresolved: list[str] = []
    for path, text in sorted(files.items()):
        if text is None:
            unresolved.append(f"{path} could not be read")
            continue
        parsed = _parse_workflow(path, text)
        facts[path] = parsed
        unresolved.extend(parsed.unresolved)

    resolved, unreached = _effective_triggers(facts)
    env_steerable: dict[str, set[str]] = {}
    oidc_environments: set[str] = set()
    ungated_oidc: list[tuple[str, str]] = []
    for path, f in facts.items():
        for env in f.environments:
            env_steerable.setdefault(env, set()).update(resolved[path])
        oidc_environments |= f.oidc_environments
        ungated_oidc.extend((path, job) for job in sorted(f.oidc_without_environment))
        if path in unreached and f.environments:
            unresolved.append(
                f"{path} is only reachable via `workflow_call` from outside this repo"
            )

    return _ReleaseSurface(
        env_steerable={e: frozenset(t) for e, t in env_steerable.items()},
        oidc_environments=frozenset(oidc_environments),
        ungated_oidc=tuple(sorted(ungated_oidc)),
        unresolved=tuple(sorted(set(unresolved))),
    )


def _pattern_covers(outer: str, inner: str) -> bool:
    """Whether every ref matching `inner` also matches `outer`.

    Both sides are GitHub ref patterns in `refs/tags/…` form, except that
    `outer` may be GitHub's `~ALL`. `fnmatch`'s `*` spans `/` where GitHub's
    does not, so an `inner` reaching across path segments is only covered by
    an `outer` that reaches as far.
    """
    if "**" in inner and "**" not in outer and outer != "~ALL":
        return False
    if outer in ALL_TAGS_PATTERNS:
        return True
    return fnmatch.fnmatchcase(inner, outer)


def _tag_pattern_gated(repo: str, pattern: str, bot_name: str) -> bool | None:
    """Whether tag rulesets stop the bot creating or moving tags matching `pattern`.

    Both `creation` and `update` must be restricted: `creation` blocks a fresh
    tag, `update` blocks re-pointing one an admin already pushed. They may come
    from different rulesets — several apply at once, and GitHub's own migration
    off tag protections split them that way.

    Returns True when both are gated, False when either is definitively not,
    None when a ruleset that might supply one could not be verified.
    """
    rulesets = _gh_json_lines(
        f"repos/{repo}/rulesets", ".[] | {id, target, enforcement}"
    )
    if rulesets is None:
        return None

    active_tag_rulesets = [
        rs
        for rs in rulesets
        if rs.get("target") == "tag"
        and rs.get("enforcement") == "active"
        and rs.get("id") is not None
    ]
    fetched = [_ruleset_detail(repo, rs["id"]) for rs in active_tag_rulesets]
    details = [d for d in fetched if d is not None]
    # An unreadable ruleset only matters where nothing readable supplies the
    # rule: there, "absent" can't be told from "not visible here".
    unreadable = len(details) != len(fetched)

    inner = f"refs/tags/{pattern}"
    unresolved = unreadable
    for rule_type in ("creation", "update"):
        gated = False
        for detail in details:
            if rule_type not in {r.get("type") for r in detail.get("rules", [])}:
                continue
            include = ((detail.get("conditions") or {}).get("ref_name") or {}).get(
                "include", []
            )
            if not any(_pattern_covers(o, inner) for o in include):
                continue
            verdict = _bypass_list_blocks_bot(detail, bot_name)
            if verdict is True:
                gated = True
                break
            unresolved = unresolved or verdict is None
        if not gated:
            return None if unresolved else False
    return True


def _required_reviewers_exclude_bot(environment: dict, bot_name: str) -> bool:
    """Whether a human other than the bot must approve deployments here.

    Required reviewers gate every trigger, ref-independent, so they are the
    one control that covers `release: published` and a steerable
    `workflow_dispatch`. Naming the bot among them hands it its own approval.
    """
    for rule in environment.get("protection_rules", []):
        if rule.get("type") != "required_reviewers":
            continue
        reviewers = rule.get("reviewers", [])
        logins = {
            (r.get("reviewer") or {}).get("login")
            for r in reviewers
            if r.get("type") == "User"
        }
        if reviewers and logins != {bot_name}:
            return True
    return False


def _environment_holds_credential(
    repo: str, env: str, mints_oidc: bool, allowed: set[str]
) -> bool | None:
    """Whether reaching this environment gets the bot something extra to spend.

    An environment with no OIDC-minting job and no secret beyond the ones the
    bot already holds at repo level is a deployment label: reaching it buys
    nothing it does not have, so gating it is not tend's business. `allowed`
    is the same allowlist `check_repo_secret_allowlist` uses — an adopter who
    moves tend's own secrets into an environment is hardening, not exposing.

    Secret *names* are readable at write access, so the nightly run sees the
    same answer a maintainer does.
    """
    if mints_oidc:
        return True
    secrets = _gh_json_lines(
        f"repos/{repo}/environments/{env}/secrets", ".secrets[] | {name}"
    )
    if secrets is None:
        return None
    return bool({s["name"] for s in secrets} - allowed)


def _environment_ref_gaps(
    repo: str,
    env: str,
    environment: dict,
    cfg: Config,
    default_branch: str,
) -> tuple[list[str], bool]:
    """Which refs this environment admits that the bot can push.

    Returns the gaps found and whether any part of the policy was
    unverifiable.
    """
    policy = environment.get("deployment_branch_policy")
    if policy is None:
        gap = (
            f"environment '{env}' has no deployment branch policy, so every ref "
            "deploys to it — including any branch or tag the bot pushes"
        )
        return [gap], False
    if policy.get("protected_branches"):
        # Verified against live GitHub: this setting rejects every tag, and
        # admits only branches carrying a *classic* protection rule — a
        # ruleset-protected branch is refused even though the branches API
        # reports it protected. The residual (a second classic-protected
        # branch the bot can push to) is in docs/security-model.md.
        return [], False

    entries = _gh_json_lines(
        f"repos/{repo}/environments/{env}/deployment-branch-policies",
        ".branch_policies[] | {name, type}",
    )
    if entries is None:
        return [], True

    admin_gated_branches = {default_branch, *cfg.protected_branches}
    gaps: list[str] = []
    unverified = False
    for entry in entries:
        pattern = entry.get("name", "")
        if entry.get("type") == "tag":
            verdict = _tag_pattern_gated(repo, pattern, cfg.bot_name)
            if verdict is False:
                gaps.append(
                    f"environment '{env}' deploys from tag pattern '{pattern}', but no "
                    "active tag ruleset the bot cannot bypass restricts `creation` and "
                    "`update` over it — so the bot can push a matching tag"
                )
            elif verdict is None:
                unverified = True
        elif pattern not in admin_gated_branches:
            gaps.append(
                f"environment '{env}' deploys from branch pattern '{pattern}', which is "
                "not a branch the merge restriction covers — the bot can push it"
            )
    return gaps, unverified


def check_release_protection(
    repo: str, cfg: Config, default_branch: str, allowed: set[str]
) -> CheckResult:
    """Check that no write-scoped actor can reach a publish or deploy.

    The merge restriction covers code that reaches the default branch through
    a merge. Nothing there touches the release path: a write-access bot can
    push a tag, cut a release, or fire a dispatch, and any of those can run a
    job holding a registry token or minting an OIDC identity. This verifies
    the chain docs/security-model.md prescribes for that path.

    Two things carry a credential past the end of a run, and each is checked
    where it lives:

    - An **environment** (its secrets, or an OIDC claim naming it). It is safe
      when required reviewers gate it, or when every ref its policy admits is
      one the bot cannot push *and* no workflow reaching it carries a trigger
      the bot can fire and steer on its own.
    - **`id-token: write` outside any environment**, where the minted token
      carries no environment claim and nothing gates the ref it comes from.

    Repo- and org-level secrets are the third, and `check_repo_secret_allowlist`
    already covers them.
    """
    name = "release-protection"
    environments = _gh_json_lines(f"repos/{repo}/environments", ".environments[]")
    if environments is None:
        return CheckResult(name, None, "Could not list environments")

    files = _fetch_workflow_files(repo)
    if files is None:
        return CheckResult(
            name, None, "Could not read .github/workflows from the default branch"
        )
    surface = _release_surface(files)

    gaps: list[str] = []
    unverified: list[str] = list(surface.unresolved)
    gated_count = 0

    for environment in environments:
        env = environment.get("name", "")
        holds = _environment_holds_credential(
            repo, env, env in surface.oidc_environments, allowed
        )
        if holds is None:
            unverified.append(f"could not list secrets for environment '{env}'")
            continue
        if not holds:
            continue
        if _required_reviewers_exclude_bot(environment, cfg.bot_name):
            gated_count += 1
            continue

        ref_gaps, ref_unverified = _environment_ref_gaps(
            repo, env, environment, cfg, default_branch
        )
        gaps.extend(ref_gaps)
        if ref_unverified:
            unverified.append(f"could not verify the ref policy of environment '{env}'")
        if ref_gaps or ref_unverified:
            continue

        # Ref-gated, so the only way left in is a trigger the bot fires and
        # steers itself against a ref the policy already allows.
        steerable = sorted(surface.env_steerable.get(env, frozenset()))
        if steerable:
            gaps.append(
                f"environment '{env}' restricts which refs deploy, but a workflow "
                f"reaching it triggers on {', '.join(f'`{t}`' for t in steerable)}, "
                "which the bot can fire, and steer, against an already-allowed ref — "
                "add required reviewers to the environment"
            )
        else:
            gated_count += 1

    if surface.ungated_oidc:
        jobs = ", ".join(f"{path}:{job}" for path, job in surface.ungated_oidc)
        gaps.append(
            f"{len(surface.ungated_oidc)} job(s) request `id-token: write` outside any "
            "environment, so the OIDC token carries no environment claim and nothing "
            "gates the ref it is minted from — the bot can mint the repository's OIDC "
            f"identity from a branch it pushes ({jobs})"
        )

    if gaps:
        detail = "\n".join(f"          - {g}" for g in gaps)
        return CheckResult(
            name,
            False,
            "A write-scoped bot token can reach a publish or deploy:\n"
            f"{detail}\n"
            "          See docs/security-model.md and the ref-protection recipe in "
            "the install-tend skill.",
        )
    if unverified:
        detail = "; ".join(sorted(set(unverified)))
        return CheckResult(name, None, f"Could not verify the release path: {detail}")
    if gated_count:
        return CheckResult(
            name,
            True,
            f"{gated_count} environment(s) holding a credential are gated against the bot",
        )
    return CheckResult(
        name, True, "No environment or workflow holds a credential the bot could spend"
    )


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

    # Engine-specific auth secret(s). Claude accepts one of two candidates
    # (OAuth token or API key); Codex takes only the API key (subscription
    # auth.json is incompatible with concurrent workflows). Verified in a
    # separate check below so the message can name the relevant secret.
    engine_auth_secrets = (
        [cfg.claude_token_secret, cfg.anthropic_api_key_secret]
        if cfg.harness == "claude"
        else [cfg.openai_key_secret]
    )
    required_secrets = [cfg.bot_token_secret]

    allowed = {cfg.bot_token_secret, *engine_auth_secrets} | set(
        cfg.allowed_repo_secrets
    )

    results = [check_branch_protection(repo, default_branch, cfg.bot_name)]
    for branch in cfg.protected_branches:
        if branch != default_branch:
            results.append(check_branch_protection(repo, branch, cfg.bot_name))
    results.append(check_bot_permission(repo, cfg.bot_name))
    results.append(check_secrets(repo, required_secrets))
    if cfg.harness == "claude":
        results.append(check_claude_auth(repo, cfg))
    else:
        results.append(check_codex_auth(repo, cfg))
    results.append(check_repo_secret_allowlist(repo, allowed))
    results.append(check_release_protection(repo, cfg, default_branch, allowed))
    return results


def check_claude_auth(repo: str, cfg: Config) -> CheckResult:
    """Claude needs either CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY —
    both being absent is the failure mode. Both being set is fine; the
    action prefers the OAuth token.
    """
    result = _gh("api", f"repos/{repo}/actions/secrets", "--jq", "[.secrets[].name]")
    if result is None:
        return CheckResult("claude-auth", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            "claude-auth", None, "Could not list secrets (may require admin access)"
        )
    try:
        names = set(json.loads(result.stdout))
    except json.JSONDecodeError:
        return CheckResult("claude-auth", None, "Could not parse secrets response")
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
        f"{cfg.anthropic_api_key_secret} is set as a repo secret.",
    )


def check_codex_auth(repo: str, cfg: Config) -> CheckResult:
    """Codex needs OPENAI_API_KEY — absence is the failure mode. The
    subscription auth.json path is not supported.
    """
    result = _gh("api", f"repos/{repo}/actions/secrets", "--jq", "[.secrets[].name]")
    if result is None:
        return CheckResult("codex-auth", None, "gh CLI not found")
    if result.returncode != 0:
        return CheckResult(
            "codex-auth", None, "Could not list secrets (may require admin access)"
        )
    try:
        names = set(json.loads(result.stdout))
    except json.JSONDecodeError:
        return CheckResult("codex-auth", None, "Could not parse secrets response")
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
