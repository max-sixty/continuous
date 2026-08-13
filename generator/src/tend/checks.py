"""Security checks for tend setup.

Verifies the two boundaries docs/security-model.md claims: the bot cannot
land code (branch protection on configured branches, bot permission level),
and a run the bot can cause reaches no credential (the `tend` environment's
deployment branch policy, every other credential-holding environment's gate,
the operational secrets living in the environment, and no repo-level secret
outside the allowlist).

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

import io
import json
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from urllib.parse import quote

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from tend.config import (
    ANTHROPIC_API_KEY_SECRET,
    BOT_TOKEN_SECRET,
    CLAUDE_TOKEN_SECRET,
    OPENAI_KEY_SECRET,
    Config,
)
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


def _bypass_actors_above_bot(actors: list[dict] | None, bot_name: str) -> bool | None:
    """Whether every bypass actor in a ruleset outranks a write-access bot.

    Returns False if one of them is the bot itself or a role at write or
    below, None when the list is withheld (only ruleset admins see
    `bypass_actors`) or names a principal this can't resolve (a team, app,
    or deploy key, or any user once `bot_name` itself fails to resolve — the
    ids have nothing to compare against). An empty list is True — nobody
    bypasses at all.
    """
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


def _ruleset_keeps_bot_out(data: dict, bot_name: str) -> bool | None:
    """Whether a fetched ruleset's bypass configuration keeps the bot out.

    Every ruleset response carries `current_user_can_bypass` — GitHub's own
    evaluation of the *caller* against the full bypass list, principals tend
    can't resolve included. When the caller is the bot, that verdict is
    exactly the question, so it outranks inferring from `bypass_actors`
    (which the API withholds below repo admin, and which the inference reads
    assuming the bot sits at write — wrong for a fixture bot that holds
    admin). Any other caller's verdict says nothing about the bot, so the
    actor-list inference is all that's left. Only "never" gates: a
    `pull_requests_only` bypass is the merge path a branch restriction
    exists to close.
    """
    if data.get("current_user_can_bypass") is not None:
        login = _current_login()
        if login is not None and _same_login(login, bot_name):
            return data["current_user_can_bypass"] == "never"
    return _bypass_actors_above_bot(data.get("bypass_actors"), bot_name)


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


def _ruleset_blocks_bot(repo: str, ruleset_id: int, bot_name: str) -> bool | None:
    """Whether a ruleset's bypass configuration keeps a write-access bot out.

    None when the ruleset is unreadable or its bypass configuration
    unverifiable.
    """
    data = _fetch_ruleset(repo, ruleset_id)
    if data is None:
        return None
    return _ruleset_keeps_bot_out(data, bot_name)


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
            "See the environment check above for how to create it."
        )
    try:
        return set(json.loads(result.stdout)), ""
    except json.JSONDecodeError:
        return None, "Could not parse environment secrets response"


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
    """The environment exists and admits only the refs the bot cannot write.

    This is the whole mechanism: a job naming the environment runs only from a
    ref in its deployment branch policy, so a workflow pushed to a feature
    branch is refused before its first step. A policy that admits anything the
    bot can push gives the secrets back.
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
        for job_id in sorted(_parse_workflow(path, text, repo).filed_deployments)
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
    external_calls: frozenset[str] = frozenset()  # job ids calling out of the repo
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


def _called_workflow(uses: str, repo: str) -> str | None:
    """The workflow file in this repo that a job-level `uses:` names, or None.

    Two spellings reach the same file: the relative form, and the
    `owner/repo/.github/workflows/x.yaml@ref` form a repo may use on its own
    workflow to pin the ref it runs. A call that leaves the repo returns None,
    not because it deploys elsewhere — the callee's jobs deploy to *this*
    repo's environments (see `_effective_triggers`) — but because its file
    cannot be read from here, which `_credential_surface` reports as unread
    rather than passes over.
    """
    relative = "./.github/workflows/"
    if uses.startswith(relative):
        return uses[len(relative) :]
    absolute = f"{repo}/.github/workflows/"
    if repo and uses.casefold().startswith(absolute.casefold()):
        return uses[len(absolute) :].partition("@")[0]
    return None


def _parse_workflow(path: str, text: str, repo: str) -> _WorkflowFacts:
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
            called = _called_workflow(uses, repo) if isinstance(uses, str) else None
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


def _credential_surface(
    repo: str, files: dict[str, str | None] | None
) -> _CredentialSurface:
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
        facts[path] = _parse_workflow(path, text, repo)

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
                f"{path} runs on {triggers} and calls another repo's workflow "
                f"({jobs}), whose jobs deploy to this repo's environments"
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

    surface = _credential_surface(repo, _fetch_workflow_files(repo))
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
        if not secrets.stdout.split() and env_name not in surface.oidc_environments:
            continue
        holders.append(env_name)
        if env_name == TEND_ENVIRONMENT:
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
            surface.env_steerable.get(env_name, frozenset()),
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
    org_secrets, org_forbidden = _list_org_secrets(org) if org else (None, False)
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


def admitted_refs(results: list[CheckResult]) -> list[str]:
    """The refs the environment may admit, read off the branch-protection runs.

    Every admitted ref must be one the bot cannot write, so the admitted set is
    exactly the branches whose protection check *passed* — not the branches the
    config names. A configured branch that does not exist yet answers 404, which
    the protection check reports as unverified; admitting it would name a ref the
    bot can then create, and the merge restriction gates `update`, not
    `creation`, so nothing would stop it carrying a workflow that reads the
    secrets. Deriving both the check and the fix from one list also keeps them
    from disagreeing about what the policy should say.
    """
    prefix = "branch-protection:"
    return list(
        dict.fromkeys(
            r.name[len(prefix) :]
            for r in results
            if r.name.startswith(prefix) and r.passed is True
        )
    )


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
    required_secrets = [BOT_TOKEN_SECRET]

    # The operational secrets are deliberately absent from `allowed`: they
    # belong to the environment, and a copy left at repo level is readable by
    # any workflow the bot can push, which is exactly the hole the environment
    # closes. The allowlist check therefore flags them as unexpected.
    allowed = set(cfg.allowed_repo_secrets)

    results = [check_branch_protection(repo, default_branch, cfg.bot_name)]
    for branch in cfg.protected_branches:
        if branch != default_branch:
            results.append(check_branch_protection(repo, branch, cfg.bot_name))
    results.append(check_bot_permission(repo, cfg.bot_name))
    admitted = admitted_refs(results)
    results.append(check_environment(repo, admitted))
    results.append(check_environment_deployments(repo))
    results.append(check_credential_environments(repo, cfg, admitted))
    results.append(check_secrets(repo, required_secrets))
    if cfg.harness == "claude":
        results.append(check_claude_auth(repo))
    else:
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
    """Codex needs OPENAI_API_KEY — absence is the failure mode. The
    subscription auth.json path is not supported.
    """
    names, err = _env_secret_names(repo)
    if names is None:
        return CheckResult("codex-auth", None, err)
    if OPENAI_KEY_SECRET in names:
        return CheckResult(
            "codex-auth",
            True,
            f"Codex auth secret present: {OPENAI_KEY_SECRET}",
        )
    return CheckResult(
        "codex-auth",
        False,
        f"Codex harness selected but {OPENAI_KEY_SECRET} "
        f"is not set in the '{TEND_ENVIRONMENT}' environment.",
    )
