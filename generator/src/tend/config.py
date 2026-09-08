"""Read and validate .config/tend.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import click
from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, Node, SequenceNode

# ruamel.yaml parses YAML 1.2 by default, which fixes PyYAML's `on:` → True
# trap and the Norway problem (yes/no/on/off coerced to bool).
_YAML = YAML(typ="safe", pure=True)


def _has_yaml_merge_key(node: Node | None, seen: set[int] | None = None) -> bool:
    """Return whether a parsed YAML tree contains a `<<` merge key."""
    if node is None:
        return False
    if seen is None:
        seen = set()
    if id(node) in seen:
        return False
    seen.add(id(node))

    if isinstance(node, MappingNode):
        for key, value in node.value:
            if key.tag == "tag:yaml.org,2002:merge":
                return True
            if _has_yaml_merge_key(key, seen) or _has_yaml_merge_key(value, seen):
                return True
    elif isinstance(node, SequenceNode):
        return any(_has_yaml_merge_key(value, seen) for value in node.value)
    return False


STANDARD_WORKFLOWS = {
    "review",
    "mention",
    "triage",
    "ci-fix",
    "nightly",
    "weekly",
    "notifications",
    "review-runs",
}
KNOWN_WORKFLOWS = {
    *STANDARD_WORKFLOWS,
    # Generated whenever at least one workflow uses Codex. It still honors
    # the common workflow enabled/override contract.
    "codex-auth-refresh",
    # install-test is opt-in via `tend init --with-install-test` but still
    # honors workflow_extra / jobs overrides from .config/tend.yaml.
    "install-test",
}
KNOWN_TOP_LEVEL = {
    "bot_name",
    "enabled",
    "memory_gist",
    "harness",
    "model",
    "effort",
    "args",
    "protected_branches",
    "secrets",
    "setup",
    "sandbox_setup",
    "sandbox_env",
    "sandbox_path",
    "workflows",
}
KNOWN_HARNESSES = {"claude", "codex"}
KNOWN_SECRETS_KEYS = {"allowed"}

# The operational secrets, by fixed name. Claude reads the OAuth token
# (subscription) or the API key (console.anthropic.com) — adopters set one;
# Codex reads either the OpenAI key or an access-only ChatGPT auth bundle.
# Not configurable: `install-tend` creates the
# `tend` environment and fills it from scratch, so there is no pre-existing
# secret whose name an adopter would want to keep.
BOT_TOKEN_SECRET = "TEND_BOT_TOKEN"
CLAUDE_TOKEN_SECRET = "CLAUDE_CODE_OAUTH_TOKEN"
ANTHROPIC_API_KEY_SECRET = "ANTHROPIC_API_KEY"
OPENAI_KEY_SECRET = "OPENAI_API_KEY"
CODEX_AUTH_SECRET = "CODEX_AUTH_JSON"
CODEX_REFRESH_AUTH_SECRET = "CODEX_REFRESH_AUTH_JSON"
CODEX_REFRESH_PAT_SECRET = "CODEX_REFRESH_PAT"
MEMORY_GIST_SECRET = "TEND_MEMORY_GIST_ID"
OPERATIONAL_SECRETS = {
    MEMORY_GIST_SECRET,
    BOT_TOKEN_SECRET,
    CLAUDE_TOKEN_SECRET,
    ANTHROPIC_API_KEY_SECRET,
    OPENAI_KEY_SECRET,
    CODEX_AUTH_SECRET,
    CODEX_REFRESH_AUTH_SECRET,
    CODEX_REFRESH_PAT_SECRET,
}
# Keys that once renamed those secrets. A leftover one is refused rather
# than warned past: ignoring it would generate workflows reading the fixed
# name while the adopter's secret still answers to the old one, and every
# job would fail on an empty token.
REMOVED_SECRETS_KEYS = {
    "bot_token": BOT_TOKEN_SECRET,
    "claude_token": CLAUDE_TOKEN_SECRET,
    "anthropic_api_key": ANTHROPIC_API_KEY_SECRET,
    "openai_key": OPENAI_KEY_SECRET,
}
_GITHUB_USERNAME = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")
# POSIX-ish env var name: letters, digits, underscore; not starting with a digit.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Env names an adopter's `sandbox_env` may NOT set. These carry the sandbox's
# credential isolation and routing — letting an adopter override them (via a
# committed config, but also as a defense against a hand-edited workflow) could
# redirect the agent's traffic off the injecting proxy or clobber the dummy
# credentials the proxy swaps for the real secrets. `PATH` is reserved too:
# use `sandbox_path` (which prepends to the fixed base) instead of replacing it.
# Kept in sync with RESERVED_SANDBOX_ENV in proxy/setup_sandbox.py — the
# `sandbox-env-reserved-parity` pre-commit hook fails the commit on drift.
RESERVED_SANDBOX_ENV = {
    "HOME",
    "PATH",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "https_proxy",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_WORKSPACE",
    "CLAUDE_CODE_REMOTE",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_AUTH_JSON",
    "CODEX_HOME",
}


ALLOWED_STEP_FIELDS = {
    "uses",
    "run",
    "name",
    "id",
    "if",
    "with",
    "env",
    "shell",
    "working-directory",
    "continue-on-error",
    "timeout-minutes",
}
DICT_STEP_FIELDS = {"with", "env"}


@dataclass
class SetupStep:
    """A single project setup step, mirroring GitHub's step schema.

    Exactly one of `uses` or `run`, plus any of `with`, `env`, `name`,
    `id`, `shell`, `working-directory`, `continue-on-error`,
    `timeout-minutes`, `if`. In the workflows that pre-check whether the
    agent needs to boot, the renderer adds that check to every step's `if:`.
    A step's own condition narrows the check. For multi-step setup, add
    multiple entries to the `setup:` list — or reference a local composite
    action with `uses`.
    """

    fields: dict


@dataclass
class WorkflowConfig:
    enabled: bool = True
    prompt: str = ""
    cron: str = ""
    watched_workflows: list[str] | None = None
    branches: list[str] | None = None
    workflow_extra: dict | None = None
    jobs: dict[str, dict] | None = None
    # Per-workflow harness override. Lets adopters trial a new harness on
    # a single workflow (e.g. `codex` on nightly only) before flipping the
    # whole bot. None means inherit from top-level `harness`.
    harness: str | None = None
    # Per-workflow model override. None inherits the top-level model when the
    # harness is unchanged, or uses the target harness's default when it changes.
    model: str | None = None
    # Per-workflow effort and CLI argument overrides. None means inherit from
    # the top level; an empty args list clears top-level arguments.
    effort: str | None = None
    args: list[str] | None = None


# Claude model allowlist — the set is small and stable enough that a
# typo-catching gate at config load is worth the maintenance.
# Codex models are NOT enumerated here: Codex's catalog churns
# (gpt-5.1-codex was current at harness bring-up; gone by the next month),
# and a stale allowlist would silently block adopters from picking a newer
# model. We pass any user-supplied string through and let `codex exec` error
# at runtime if it's wrong.
KNOWN_MODELS_BY_HARNESS = {
    "claude": {"opus", "sonnet", "haiku"},
}
DEFAULT_MODEL_BY_HARNESS = {
    "claude": "opus",
    # Let the pinned Codex CLI select the recommended model for the account.
    "codex": "",
}


def _effective_model(
    harness: str,
    model: str,
    workflow_harness: str | None,
    workflow_model: str | None,
) -> str:
    """Resolve a workflow's model, resetting defaults when its harness changes."""
    if workflow_model is not None:
        return workflow_model
    if workflow_harness is not None and workflow_harness != harness:
        return DEFAULT_MODEL_BY_HARNESS[workflow_harness]
    return model


# Empty string leaves effort at the harness CLI's model-specific default.
KNOWN_EFFORTS_BY_HARNESS = {
    "claude": {"", "low", "medium", "high", "xhigh", "max"},
    "codex": {"", "low", "medium", "high", "xhigh"},
}


@dataclass
class Config:
    bot_name: str
    default_branch: str
    protected_branches: list[str]
    harness: str
    model: str
    effort: str
    setup: list[SetupStep]
    workflows: dict[str, WorkflowConfig]
    # Exact additional argv elements passed to the selected harness CLI.
    args: list[str] = field(default_factory=list)
    # Runtime kill switch. Generated workflows stay installed and read this
    # value from the default branch at the start of every operational job.
    enabled: bool = True
    config_path: str = ".config/tend.yaml"
    # Owner of the repo where workflows will run. Used to gate jobs that fail
    # noisily on forks (no access to bot/Claude secrets). Not user-configurable;
    # cli.init populates this via `gh repo view` so fork-based maintainer
    # workflows still get the canonical owner. Empty means "skip the guard"
    # (gh unavailable, or no default repo configured).
    repo_owner: str = ""
    allowed_repo_secrets: list[str] = field(default_factory=list)
    # Adopter levers that reach inside either harness's sandbox, before the
    # agent launches (runner-side `setup:` doesn't — it runs as the runner user
    # around the composite action). `sandbox_path` prepends dirs to the sandbox
    # PATH; `sandbox_env` adds NAME=VALUE pairs to the agent's launch env;
    # `sandbox_setup` runs shell commands as the sandbox user.
    sandbox_path: list[str] = field(default_factory=list)
    sandbox_env: dict[str, str] = field(default_factory=dict)
    sandbox_setup: list[str] = field(default_factory=list)
    # Opt-in experiment that persists Claude Code's model-authored auto memory
    # in a bot-owned secret Gist. The Gist ID stays in a fixed environment
    # secret so a public repository does not publish the unlisted URL.
    memory_gist: bool = False

    def enabled_harnesses(self) -> set[str]:
        """Harnesses used by the workflows a normal regeneration emits."""
        return _enabled_harnesses(self.harness, self.workflows)

    def default_prompt(self, skill: str, args: str = "") -> str:
        """Default prompt invoking a tend-ci-runner skill in harness-native syntax.

        Claude resolves `/tend-ci-runner:NAME` as a slash command. Codex resolves
        `$NAME` as a skill mention (or matches by description); the
        `tend-ci-runner` namespace prefix isn't needed at the prompt site because
        skill names within the plugin are unique. `args` is appended raw so
        callers can splice their own placeholders (`{pr_number}` etc.) and run
        the existing replace step.
        """
        prefix = f"/tend-ci-runner:{skill}" if self.harness == "claude" else f"${skill}"
        return f"{prefix} {args}".rstrip()

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        if path is None:
            path = Path(".config/tend.yaml")
        if not path.exists():
            legacy = Path(".config/tend.toml")
            if path == Path(".config/tend.yaml") and legacy.exists():
                raise click.ClickException(
                    f"Found {legacy} but tend now reads {path}. "
                    "Run `uvx tend@latest init` to migrate "
                    "(verifies the parsed config is equivalent, swaps the file, "
                    "and regenerates workflows in one step)."
                )
            raise click.ClickException(f"Config not found: {path}")
        text = path.read_text(encoding="utf-8")
        if _has_yaml_merge_key(_YAML.compose(text)):
            raise click.ClickException("YAML merge keys (<<) are not supported")
        raw = _YAML.load(text) or {}

        if not isinstance(raw, dict):
            raise click.ClickException(
                f"{path} must contain a YAML mapping at the top level"
            )

        if "bot_name" not in raw:
            raise click.ClickException("Missing required field: bot_name")

        bot_name = raw["bot_name"]
        if not isinstance(bot_name, str) or not bot_name:
            raise click.ClickException("bot_name must not be empty")
        if not _GITHUB_USERNAME.match(bot_name):
            raise click.ClickException(
                f"bot_name '{bot_name}' is not a valid GitHub username "
                "(only letters, digits, and hyphens)"
            )

        harness = raw.get("harness", "claude")
        if harness not in KNOWN_HARNESSES:
            raise click.ClickException(
                f"harness '{harness}' is not recognized "
                f"(known: {', '.join(sorted(KNOWN_HARNESSES))})"
            )

        model = raw.get("model", DEFAULT_MODEL_BY_HARNESS[harness])
        known_models = KNOWN_MODELS_BY_HARNESS.get(harness)
        if known_models is not None and model not in known_models:
            raise click.ClickException(
                f"model '{model}' is not recognized for harness '{harness}' "
                f"(known: {', '.join(sorted(known_models))})"
            )

        effort = _parse_effort(raw.get("effort", ""), harness, model, "effort")

        args = _parse_args(raw.get("args", []), "args")

        memory_gist = raw.get("memory_gist", False)
        if not isinstance(memory_gist, bool):
            raise click.ClickException("memory_gist must be true or false")

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise click.ClickException("enabled must be true or false")

        unknown = set(raw.keys()) - KNOWN_TOP_LEVEL
        for key in sorted(unknown):
            click.echo(f"Warning: unknown config key '{key}'", err=True)

        protected_branches = raw.get("protected_branches", [])
        if not isinstance(protected_branches, list) or not all(
            isinstance(b, str) and b for b in protected_branches
        ):
            raise click.ClickException(
                "protected_branches must be a list of non-empty strings"
            )

        secrets = raw.get("secrets", {}) or {}
        removed = sorted(set(secrets) & set(REMOVED_SECRETS_KEYS))
        if removed:
            renames = ", ".join(
                f"secrets.{key} → {REMOVED_SECRETS_KEYS[key]}" for key in removed
            )
            raise click.ClickException(
                f"Removed secret name override(s): {renames}. The operational "
                "secret names are fixed — rename each secret to the name shown "
                "and drop the key."
            )
        unknown_secrets = set(secrets) - KNOWN_SECRETS_KEYS
        for key in sorted(unknown_secrets):
            click.echo(f"Warning: unknown secrets key '{key}'", err=True)

        setup: list[SetupStep] = []
        for i, entry in enumerate(raw.get("setup", []) or []):
            if not isinstance(entry, dict):
                raise click.ClickException(
                    f"setup[{i}] must be a mapping with `uses` or `run`"
                )
            if "raw" in entry:
                raise click.ClickException(
                    f"setup[{i}]: `raw` was removed. Split into multiple "
                    "setup entries, or move the YAML into a local "
                    "composite action and reference it with `uses`."
                )
            unknown = set(entry.keys()) - ALLOWED_STEP_FIELDS
            if unknown:
                raise click.ClickException(
                    f"setup[{i}]: unknown field(s): {', '.join(sorted(unknown))}. "
                    f"Allowed: {', '.join(sorted(ALLOWED_STEP_FIELDS))}."
                )
            step_keys = {"uses", "run"} & entry.keys()
            if len(step_keys) != 1:
                raise click.ClickException(
                    f"setup[{i}] must have exactly one of `uses` or `run`"
                )
            for k in DICT_STEP_FIELDS:
                if k in entry and not isinstance(entry[k], dict):
                    raise click.ClickException(f"setup[{i}]: `{k}` must be a mapping")
            if "if" in entry:
                condition = entry["if"]
                if not isinstance(condition, str) or not condition.strip():
                    raise click.ClickException(
                        f"setup[{i}]: `if` must be a non-empty string; quote "
                        'it (`if: "false"`) so YAML does not read it as a '
                        "boolean, number, or list"
                    )
                condition = condition.strip()
                if condition.startswith("${{") and condition.endswith("}}"):
                    condition = condition[3:-2].strip()
                    if not condition:
                        raise click.ClickException(
                            f"setup[{i}]: `if` must contain an expression"
                        )
                if "${{" in condition or "}}" in condition:
                    raise click.ClickException(
                        f"setup[{i}]: `if` must be a plain expression or one "
                        "whole `${{ ... }}` expression"
                    )
                entry = {**entry, "if": condition}
            setup.append(SetupStep(fields=dict(entry)))

        sandbox_path = raw.get("sandbox_path", []) or []
        if not isinstance(sandbox_path, list) or not all(
            isinstance(d, str) and d for d in sandbox_path
        ):
            raise click.ClickException(
                "sandbox_path must be a list of non-empty strings "
                '(e.g. sandbox_path: ["~/.cargo/bin"]); '
                "`~` expands to the sandbox home"
            )
        # A newline in a dir would drop an un-indented continuation line into
        # the rendered `|` block scalar (which has no indent() filter),
        # terminating it and breaking the workflow — fail at `init` instead.
        if any("\n" in d for d in sandbox_path):
            raise click.ClickException(
                "sandbox_path entries must each be a single line"
            )

        sandbox_env_raw = raw.get("sandbox_env", {}) or {}
        if not isinstance(sandbox_env_raw, dict):
            raise click.ClickException(
                "sandbox_env must be a mapping of NAME: VALUE "
                '(e.g. sandbox_env: {RUST_BACKTRACE: "1"})'
            )
        sandbox_env: dict[str, str] = {}
        for name, value in sandbox_env_raw.items():
            if not isinstance(name, str) or not _ENV_NAME.match(name):
                raise click.ClickException(
                    f"sandbox_env key '{name}' is not a valid environment "
                    "variable name (letters, digits, underscore; not starting "
                    "with a digit)"
                )
            if name in RESERVED_SANDBOX_ENV:
                hint = (
                    " Use `sandbox_path` to extend PATH."
                    if name == "PATH"
                    else " It carries the sandbox's credential isolation and "
                    "cannot be overridden."
                )
                raise click.ClickException(
                    f"sandbox_env may not set reserved key '{name}'.{hint}"
                )
            # Coerce a YAML scalar (1, true) to its string form; reject a
            # non-scalar (a list/dict would otherwise str() into a Python repr
            # and silently smuggle garbage into the agent env line). `bool` is
            # an `int` subclass, so handle it first and emit the shell-
            # conventional lowercase rather than Python's `True`/`False`.
            if isinstance(value, bool):
                coerced = "true" if value else "false"
            elif isinstance(value, str):
                coerced = value
            elif isinstance(value, (int, float)):
                coerced = str(value)
            else:
                raise click.ClickException(
                    f"sandbox_env value for '{name}' must be a scalar "
                    "(string, number, or boolean)"
                )
            # The action splits this input one NAME=VALUE pair per line, so a
            # value carrying a newline would be read as a pair and a malformed
            # line rather than one value — fail at `init` instead. (The block
            # scalar itself is safe: `block_input` indents a continuation line
            # like any other.)
            if "\n" in coerced:
                raise click.ClickException(
                    f"sandbox_env value for '{name}' must be a single line"
                )
            sandbox_env[name] = coerced

        sandbox_setup = raw.get("sandbox_setup", []) or []
        if not isinstance(sandbox_setup, list) or not all(
            isinstance(c, str) and c.strip() for c in sandbox_setup
        ):
            raise click.ClickException(
                "sandbox_setup must be a list of non-empty shell command strings "
                '(e.g. sandbox_setup: ["rustup component add clippy"])'
            )

        workflows: dict[str, WorkflowConfig] = {}
        for name, wf_raw in (raw.get("workflows") or {}).items():
            if name == "renovate":
                raise click.ClickException(
                    "workflows.renovate has been renamed to workflows.weekly"
                )
            if name not in KNOWN_WORKFLOWS:
                click.echo(
                    f"Warning: unknown workflow '{name}' in config (known: {', '.join(sorted(KNOWN_WORKFLOWS))})",
                    err=True,
                )
            if isinstance(wf_raw, dict):
                watched = wf_raw.get("watched_workflows")
                branches = wf_raw.get("branches")
                # Both render straight into the `workflow_run:` trigger through
                # `tojson`, so an unchecked value reaches the workflow file
                # verbatim: `watched_workflows: ci` becomes `workflows: "ci"`,
                # which GitHub matches against nothing, and ci-fix silently
                # never fires. A number gets no further than `len()` below,
                # which raises a bare TypeError instead of a config error.
                for key, value, example in (
                    ("watched_workflows", watched, '["ci"]'),
                    ("branches", branches, '["main"]'),
                ):
                    if value is not None and (
                        not isinstance(value, list)
                        or not all(isinstance(s, str) and s for s in value)
                    ):
                        raise click.ClickException(
                            f"workflows.{name}.{key} must be a list of "
                            f"non-empty strings (e.g. {key}: {example})"
                        )
                if branches is not None and not branches:
                    raise click.ClickException(
                        f"workflows.{name}.branches: [] matches no branch — "
                        "omit the key to default to the repository's default "
                        "branch, or list the branches to watch."
                    )
                if watched is not None and len(watched) == 0 and name == "ci-fix":
                    raise click.ClickException(
                        "watched_workflows: [] is invalid for ci-fix — "
                        "workflow_run requires at least one workflow name. "
                        "Disable ci-fix with enabled: false instead."
                    )
                workflow_extra = wf_raw.get("workflow_extra")
                if workflow_extra is not None and not isinstance(workflow_extra, dict):
                    raise click.ClickException(
                        f"workflows.{name}.workflow_extra must be a mapping"
                    )
                jobs_raw = wf_raw.get("jobs")
                if jobs_raw is not None and (
                    not isinstance(jobs_raw, dict)
                    or not all(isinstance(v, dict) for v in jobs_raw.values())
                ):
                    raise click.ClickException(
                        f"workflows.{name}.jobs must be a mapping of mappings"
                    )
                wf_harness = wf_raw.get("harness")
                wf_model = wf_raw.get("model")
                wf_args = (
                    _parse_args(wf_raw["args"], f"workflows.{name}.args")
                    if "args" in wf_raw
                    else None
                )
                if wf_harness is not None and wf_harness not in KNOWN_HARNESSES:
                    raise click.ClickException(
                        f"workflows.{name}.harness '{wf_harness}' is not recognized "
                        f"(known: {', '.join(sorted(KNOWN_HARNESSES))})"
                    )
                eff_harness = wf_harness or harness
                eff_model = _effective_model(harness, model, wf_harness, wf_model)
                wf_effort = (
                    _parse_effort(
                        wf_raw["effort"],
                        eff_harness,
                        eff_model,
                        f"workflows.{name}.effort",
                    )
                    if "effort" in wf_raw
                    else None
                )
                # Validate an explicit per-workflow model against the effective
                # harness. A harness change without one uses that harness's
                # default instead of carrying a model across families.
                if wf_harness is not None or wf_model is not None:
                    eff_known = KNOWN_MODELS_BY_HARNESS.get(eff_harness)
                    if eff_known is not None and eff_model not in eff_known:
                        raise click.ClickException(
                            f"workflows.{name} harness '{eff_harness}' is incompatible "
                            f"with model '{eff_model}' "
                            f"(known for {eff_harness}: {', '.join(sorted(eff_known))}). "
                            f"Set `workflows.{name}.model:` (or change the top-level "
                            "`model:`) to a valid value for this harness."
                        )

                if wf_effort is None and (
                    wf_harness is not None or wf_model is not None
                ):
                    _parse_effort(
                        effort,
                        eff_harness,
                        eff_model,
                        "effort",
                        inherited_by=f"workflows.{name}",
                    )
                wf_prompt = wf_raw.get("prompt", "")
                if wf_prompt is None:  # `prompt:` with nothing after it
                    wf_prompt = ""
                if not isinstance(wf_prompt, str):
                    raise click.ClickException(
                        f"workflows.{name}.prompt must be a string, "
                        f"got {type(wf_prompt).__name__}"
                    )
                # `mention` builds its prompt from the triggering event —
                # which of five comment shapes fired, the queue delay, the
                # ids to read back — so there is no text an override could
                # replace without breaking the dispatch. Refuse it rather
                # than accept a key that renders nowhere.
                if wf_prompt and name == "mention":
                    raise click.ClickException(
                        "workflows.mention.prompt is not supported: mention "
                        "composes its prompt from the triggering event. Put "
                        "standing guidance in the repo's `running-tend` skill "
                        "overlay instead."
                    )
                # A whitespace-only prompt is truthy, so it beats the default
                # and leaves the agent step with no instructions — which the
                # Claude action fails on by name and the Codex action hands to
                # `codex exec` and runs. `""` and a bare `prompt:` are falsy and
                # fall through to the default instead, which is quieter but no
                # more what the adopter wrote. All three are typos; refuse them
                # here, where the key's presence still tells them apart from an
                # absent one.
                if "prompt" in wf_raw and not wf_prompt.strip():
                    raise click.ClickException(
                        f"workflows.{name}.prompt is blank. Drop the key to "
                        f"use the default prompt."
                    )
                workflows[name] = WorkflowConfig(
                    enabled=wf_raw.get("enabled", True),
                    prompt=wf_prompt,
                    cron=wf_raw.get("cron", ""),
                    watched_workflows=watched,
                    branches=branches,
                    workflow_extra=workflow_extra,
                    jobs=jobs_raw,
                    harness=wf_harness,
                    model=wf_model,
                    effort=wf_effort,
                    args=wf_args,
                )
            else:
                workflows[name] = WorkflowConfig(enabled=bool(wf_raw))

        # Both harnesses run behind the same credential-isolation sandbox;
        # these levers therefore apply to either one.
        enabled_harnesses = _enabled_harnesses(harness, workflows)
        if memory_gist and "claude" not in enabled_harnesses:
            raise click.ClickException(
                "memory_gist is experimental and requires at least one enabled "
                "workflow using the Claude harness"
            )

        allowed = secrets.get("allowed", [])
        if not isinstance(allowed, list) or not all(
            isinstance(s, str) for s in allowed
        ):
            raise click.ClickException(
                "secrets.allowed must be a list of strings, "
                'e.g. allowed: ["CODECOV_TOKEN"]'
            )

        # The allowlist is the one deliberate exception to "no repo-level
        # secrets", for tokens whose exposure the maintainer accepts. The
        # operational secrets are never that: allowlisting one would let a
        # repo-level copy pass `tend check`, handing any workflow the bot
        # pushes exactly what the environment gate denies.
        blessed = OPERATIONAL_SECRETS & set(allowed)
        if blessed:
            raise click.ClickException(
                f"secrets.allowed must not include {', '.join(sorted(blessed))}: "
                "the operational secrets live in the 'tend' environment, and "
                "allowlisting a repo-level copy would let any workflow the bot "
                "pushes read it."
            )

        return cls(
            bot_name=bot_name,
            default_branch="main",
            protected_branches=protected_branches,
            harness=harness,
            model=model,
            effort=effort,
            args=args,
            setup=setup,
            sandbox_path=sandbox_path,
            sandbox_env=sandbox_env,
            sandbox_setup=sandbox_setup,
            memory_gist=memory_gist,
            enabled=enabled,
            workflows=workflows,
            allowed_repo_secrets=allowed,
        )


def _parse_args(raw: object, key: str) -> list[str]:
    """Validate exact CLI argument elements from one config key."""
    if not isinstance(raw, list) or not all(
        isinstance(arg, str)
        and arg.strip()
        and "\n" not in arg
        and "\r" not in arg
        and arg == arg.rstrip()
        for arg in raw
    ):
        raise click.ClickException(
            f"{key} must be a list of non-blank, single-line strings "
            "without trailing whitespace "
            '(e.g. ["--max-turns", "50"])'
        )
    return list(raw)


def _parse_effort(
    raw: object,
    harness: str,
    model: str,
    key: str,
    *,
    inherited_by: str | None = None,
) -> str:
    """Validate an effort value against the CLI and model selected for it."""
    source = key if inherited_by is None else f"{key} (inherited by {inherited_by})"
    known = KNOWN_EFFORTS_BY_HARNESS[harness]
    if not isinstance(raw, str) or raw not in known:
        raise click.ClickException(
            f"{source} '{raw}' is not recognized for harness '{harness}' "
            f"(known: {', '.join(sorted(e for e in known if e))})"
        )
    if raw and harness == "claude" and model == "haiku":
        if inherited_by is not None:
            raise click.ClickException(
                f"{source} is not supported for Claude model 'haiku'; "
                f'set `{inherited_by}.effort: ""` to use that model\'s default '
                "or drop the top-level `effort:`"
            )
        raise click.ClickException(
            f"{key} is not supported for Claude model 'haiku'; "
            "drop the key to use that model"
        )
    return raw


def _enabled_harnesses(harness: str, workflows: dict[str, WorkflowConfig]) -> set[str]:
    """Effective harnesses of enabled, normally generated workflows."""
    enabled = set()
    for name in STANDARD_WORKFLOWS:
        workflow = workflows.get(name, WorkflowConfig())
        if not workflow.enabled:
            continue
        if name == "ci-fix" and workflow.watched_workflows is None:
            continue
        enabled.add(workflow.harness or harness)
    return enabled
