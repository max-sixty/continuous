"""Refuse to run unless the default branch matches Tend's merge policy.

Shared verbatim by both harness actions (``claude/``, ``codex/``), where the
step is ``id: security`` and a non-zero exit is what the "Report failure" step
reads off ``steps.security.outcome``.

The step runs with the bot's own token, so ``current_user_can_bypass`` is
GitHub's direct answer. Maintainer mode accepts a non-bypassable update rule (or the
legacy protected-branch floor). Yolo requires the exact middle state: the bot
may bypass an update rule only through a pull request, and a separate rule
requires fresh CODEOWNER approval that the bot cannot bypass.

Decisions this encodes:

- A ruleset whose ``current_user_can_bypass`` cannot be read proves nothing
  either way, so it neither blocks nor counts as bypassable; the run falls
  through to the ``.protected`` floor if no other update rule settles it.
- Any readable value other than ``never`` counts as bypassable, JSON ``null``
  included — an answer that isn't "never" is not a restriction.
- A rules listing that cannot be read — or that comes back in a shape this
  cannot make rules out of — is treated as "no update rules apply", the same
  fallback the shell body took, so a token that cannot see rulesets still
  meets the ``.protected`` floor rather than passing unchecked.

Inputs (env): ``GITHUB_REPOSITORY``, ``TEND_MERGE``, plus the bot's
``GITHUB_TOKEN``, which reaches ``gh`` through the environment.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import _common

BYPASS_ERROR = (
    "The bot can bypass every restrict-updates ruleset on '{branch}' "
    "(current_user_can_bypass != never), so the merge restriction does not "
    "restrict the bot. Remove the bot — or any team, role, or user exemption "
    "covering it — from the rulesets' bypass actors. See docs/security-model.md "
    "in the Tend repo."
)

UNPROTECTED_ERROR = (
    "Default branch '{branch}' is NOT protected. Without branch protection, "
    "the bot can merge PRs without review. Add a branch protection rule or "
    "ruleset before using Tend. See docs/security-model.md in the Tend repo."
)

YOLO_BYPASS_ERROR = (
    "Yolo merge mode requires the bot's effective update bypass on '{branch}' to "
    "be pull_requests_only; GitHub reported {actual}. Run `tend check --fix`."
)

CONTROL_PLANE_ERROR = (
    "Yolo merge mode requires a pull-request rule on '{branch}' with fresh "
    "CODEOWNER approval that this bot cannot bypass. Run `tend check --fix` "
    "after the generated CODEOWNERS block is merged."
)
CONTROL_PLANE_OWNER_ERROR = (
    "Yolo merge mode requires control_plane_owner to be an independent "
    "GitHub user, not the Tend bot account."
)

CODEOWNERS_BEGIN = "# BEGIN tend control plane"
CODEOWNERS_END = "# END tend control plane"
CONTROL_PLANE_PATHS = (
    "/.github/**",
    "/.config/tend.yaml",
    "/CODEOWNERS",
    "/docs/CODEOWNERS",
    "**/CLAUDE.md",
    "**/CLAUDE.local.md",
    "**/AGENTS.md",
    "**/AGENTS.override.md",
    "**/.claude",
    "**/.claude/**",
    "**/.agents",
    "**/.agents/**",
)


def update_ruleset_ids(rules: Any) -> list[int]:
    """The ids of the rulesets contributing an ``update`` rule, deduped.

    A ruleset can contribute several rules to one branch, and only the
    ``update`` ones restrict who may move the branch.

    Anything that is not a rule naming both a type and a ruleset id
    contributes nothing, which is what the jq ``select`` this replaced did.
    The listing is read best-effort, so a body that is an error object rather
    than an array has to fall through to the ``.protected`` floor rather than
    abort the gate.
    """
    if not isinstance(rules, list):
        return []
    return sorted(
        {
            rule["ruleset_id"]
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("type") == "update"
            and isinstance(rule.get("ruleset_id"), int)
        }
    )


def ruleset_ids(rules: Any, rule_type: str) -> list[int]:
    """The rulesets contributing ``rule_type`` to the branch, deduped."""
    if not isinstance(rules, list):
        return []
    return sorted(
        {
            rule["ruleset_id"]
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("type") == rule_type
            and isinstance(rule.get("ruleset_id"), int)
        }
    )


def effective_update_bypass(rulesets: list[dict[str, Any] | None]) -> str | None:
    """Combine applying update rulesets from most to least restrictive."""
    bypasses = [
        ruleset.get("current_user_can_bypass")
        for ruleset in rulesets
        if isinstance(ruleset, dict)
    ]
    if "never" in bypasses:
        return "never"
    if any(ruleset is None for ruleset in rulesets):
        return None
    if "pull_requests_only" in bypasses:
        return "pull_requests_only"
    return "always"


def has_control_plane_review(rulesets: list[dict[str, Any] | None]) -> bool:
    """Whether one applying ruleset enforces the yolo control-plane review."""
    for ruleset in rulesets:
        if not isinstance(ruleset, dict):
            continue
        if ruleset.get("current_user_can_bypass") != "never":
            continue
        for rule in ruleset.get("rules", []):
            parameters = rule.get("parameters", {})
            if (
                rule.get("type") == "pull_request"
                and parameters.get("require_code_owner_review") is True
                and parameters.get("dismiss_stale_reviews_on_push") is True
            ):
                return True
    return False


def has_valid_control_plane_codeowners(repo: str, branch: str, owner: str) -> bool:
    """Whether GitHub accepts Tend's final managed CODEOWNERS block."""
    content = None
    for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        try:
            response = _common.gh_json(
                "api", f"repos/{repo}/contents/{path}?ref={quote(branch, safe='')}"
            )
        except _common.GH_READ_FAILED:
            continue
        if not isinstance(response, dict) or not isinstance(
            response.get("content"), str
        ):
            return False
        try:
            content = base64.b64decode(response["content"]).decode()
        except (ValueError, UnicodeDecodeError):
            return False
        break
    if content is None:
        return False

    block_lines = [CODEOWNERS_BEGIN]
    block_lines.extend(f"{path} {owner}" for path in CONTROL_PLANE_PATHS)
    block_lines.append(CODEOWNERS_END)
    block = "\n".join(block_lines)
    if (
        content.count(CODEOWNERS_BEGIN) != 1
        or content.count(CODEOWNERS_END) != 1
        or not content.rstrip().endswith(block)
    ):
        return False

    try:
        response = _common.gh_json(
            "api", f"repos/{repo}/codeowners/errors?ref={quote(branch, safe='')}"
        )
    except _common.GH_READ_FAILED:
        return False
    if not isinstance(response, dict) or not isinstance(response.get("errors"), list):
        return False
    begin_line = content.splitlines().index(CODEOWNERS_BEGIN) + 1
    managed_lines = range(begin_line, begin_line + len(block_lines))
    return not any(
        not isinstance(error, dict)
        or error.get("line") is None
        or error.get("line") in managed_lines
        for error in response["errors"]
    )


def main() -> int:
    env = _common.require_env("GITHUB_REPOSITORY", "TEND_MERGE")
    repo = env["GITHUB_REPOSITORY"]
    merge = env["TEND_MERGE"]
    if merge not in {"maintainer", "yolo"}:
        return _common.fail(f"Unknown Tend merge mode: {merge}")

    # The two reads the gate cannot proceed without are left to raise. A red
    # gate is the safe direction, and gh's own explanation — "Bad credentials",
    # "Not Found" — is already on stderr from `_common.gh`.
    default_branch = _common.gh_json("api", f"repos/{repo}")["default_branch"]

    # A GitHub blip can answer this with an HTML page under a 200, so the parse
    # fails rather than the call: catching only the non-zero exit would abort
    # the gate on an outage, and "Report failure" keys on this step's outcome,
    # so the outage would go unrecorded as well.
    try:
        rules = _common.gh_json("api", f"repos/{repo}/rules/branches/{default_branch}")
    except _common.GH_READ_FAILED:
        if merge == "yolo":
            return _common.fail(
                YOLO_BYPASS_ERROR.format(branch=default_branch, actual="unknown")
            )
        rules = []

    details: dict[int, dict[str, Any] | None] = {}

    def fetch(ruleset_id: int) -> dict[str, Any] | None:
        if ruleset_id in details:
            return details[ruleset_id]
        try:
            ruleset = _common.gh_json("api", f"repos/{repo}/rulesets/{ruleset_id}")
        except _common.GH_READ_FAILED:
            ruleset = None
        details[ruleset_id] = ruleset if isinstance(ruleset, dict) else None
        return details[ruleset_id]

    update_ids = update_ruleset_ids(rules)
    update_rulesets = [fetch(ruleset_id) for ruleset_id in update_ids]
    bypass = effective_update_bypass(update_rulesets) if update_ids else "always"

    if merge == "yolo":
        owner = _common.require_env("TEND_CONTROL_PLANE_OWNER")[
            "TEND_CONTROL_PLANE_OWNER"
        ]
        identity = _common.gh_json("api", "user")
        login = identity.get("login") if isinstance(identity, dict) else None
        if (
            not isinstance(login, str)
            or "/" in owner
            or owner.casefold() == f"@{login}".casefold()
        ):
            return _common.fail(CONTROL_PLANE_OWNER_ERROR)
        if not has_valid_control_plane_codeowners(repo, default_branch, owner):
            return _common.fail(CONTROL_PLANE_ERROR.format(branch=default_branch))
        if bypass != "pull_requests_only":
            return _common.fail(
                YOLO_BYPASS_ERROR.format(branch=default_branch, actual=bypass)
            )
        pull_request_rulesets = [
            fetch(ruleset_id) for ruleset_id in ruleset_ids(rules, "pull_request")
        ]
        if not has_control_plane_review(pull_request_rulesets):
            return _common.fail(CONTROL_PLANE_ERROR.format(branch=default_branch))
        print(
            "Security preflight passed: yolo may merge pull requests, direct "
            f"pushes to '{default_branch}' are blocked, and control-plane "
            "changes require maintainer approval",
            flush=True,
        )
        return 0

    if bypass == "never":
        print(
            "Security preflight passed: bot cannot bypass the restrict-updates "
            f"ruleset on '{default_branch}'",
            flush=True,
        )
        return 0
    if any(
        isinstance(ruleset, dict) and ruleset.get("current_user_can_bypass") != "never"
        for ruleset in update_rulesets
    ):
        return _common.fail(BYPASS_ERROR.format(branch=default_branch))

    # No update rules apply (or none were readable): fall back to requiring
    # that the branch is protected at all, e.g. by required reviews.
    branch = _common.gh_json("api", f"repos/{repo}/branches/{default_branch}")
    if branch.get("protected") is not True:
        return _common.fail(UNPROTECTED_ERROR.format(branch=default_branch))
    print(
        f"Security preflight passed: default branch '{default_branch}' is protected",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    _common.run(main)
