"""Read and validate .config/tend.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import click

KNOWN_WORKFLOWS = {
    "review",
    "mention",
    "triage",
    "ci-fix",
    "nightly",
    "weekly",
    "notifications",
    "review-runs",
}
KNOWN_TOP_LEVEL = {
    "bot_name",
    "model",
    "protected_branches",
    "secrets",
    "setup",
    "workflows",
}
KNOWN_SECRETS_KEYS = {"bot_token", "claude_token", "allowed"}
_GITHUB_USERNAME = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")


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
    `timeout-minutes`, `if`. The renderer injects the notifications
    pre-check `if:` guard when absent. For multi-step setup, add multiple
    `[[setup]]` entries — or reference a local composite action with `uses`.
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


KNOWN_MODELS = {"opus", "sonnet", "haiku"}


@dataclass
class Config:
    bot_name: str
    default_branch: str
    protected_branches: list[str]
    bot_token_secret: str
    claude_token_secret: str
    model: str
    setup: list[SetupStep]
    workflows: dict[str, WorkflowConfig]
    allowed_repo_secrets: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        if path is None:
            path = Path(".config/tend.toml")
        if not path.exists():
            raise click.ClickException(f"Config not found: {path}")
        with path.open("rb") as f:
            raw = tomllib.load(f)

        if "bot_name" not in raw:
            raise click.ClickException("Missing required field: bot_name")

        bot_name = raw["bot_name"]
        if not bot_name:
            raise click.ClickException("bot_name must not be empty")
        if not _GITHUB_USERNAME.match(bot_name):
            raise click.ClickException(
                f"bot_name '{bot_name}' is not a valid GitHub username "
                "(only letters, digits, and hyphens)"
            )

        model = raw.get("model", "opus")
        if model not in KNOWN_MODELS:
            raise click.ClickException(
                f"model '{model}' is not recognized (known: {', '.join(sorted(KNOWN_MODELS))})"
            )

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

        secrets = raw.get("secrets", {})
        unknown_secrets = set(secrets.keys()) - KNOWN_SECRETS_KEYS
        for key in sorted(unknown_secrets):
            click.echo(f"Warning: unknown secrets key '{key}'", err=True)

        setup: list[SetupStep] = []
        for i, entry in enumerate(raw.get("setup", [])):
            if not isinstance(entry, dict):
                raise click.ClickException(
                    f"setup[{i}] must be a table with `uses` or `run`"
                )
            if "raw" in entry:
                raise click.ClickException(
                    f"setup[{i}]: `raw` was removed. Split into multiple "
                    "[[setup]] entries, or move the YAML into a local "
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
                    raise click.ClickException(f"setup[{i}]: `{k}` must be a table")
            setup.append(SetupStep(fields=dict(entry)))

        workflows: dict[str, WorkflowConfig] = {}
        for name, wf_raw in raw.get("workflows", {}).items():
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
                if watched is not None and len(watched) == 0 and name == "ci-fix":
                    raise click.ClickException(
                        "watched_workflows = [] is invalid for ci-fix — "
                        "workflow_run requires at least one workflow name. "
                        "Disable ci-fix with enabled = false instead."
                    )
                workflow_extra = wf_raw.get("workflow_extra")
                if workflow_extra is not None and not isinstance(workflow_extra, dict):
                    raise click.ClickException(
                        f"workflows.{name}.workflow_extra must be a table"
                    )
                jobs_raw = wf_raw.get("jobs")
                if jobs_raw is not None and (
                    not isinstance(jobs_raw, dict)
                    or not all(isinstance(v, dict) for v in jobs_raw.values())
                ):
                    raise click.ClickException(
                        f"workflows.{name}.jobs must be a table of tables"
                    )
                workflows[name] = WorkflowConfig(
                    enabled=wf_raw.get("enabled", True),
                    prompt=wf_raw.get("prompt", ""),
                    cron=wf_raw.get("cron", ""),
                    watched_workflows=watched,
                    branches=wf_raw.get("branches"),
                    workflow_extra=workflow_extra,
                    jobs=jobs_raw,
                )
            else:
                workflows[name] = WorkflowConfig(enabled=bool(wf_raw))

        allowed = secrets.get("allowed", [])
        if not isinstance(allowed, list) or not all(
            isinstance(s, str) for s in allowed
        ):
            raise click.ClickException(
                "secrets.allowed must be a list of strings, "
                'e.g. allowed = ["CODECOV_TOKEN"]'
            )

        return cls(
            bot_name=bot_name,
            default_branch="main",
            protected_branches=protected_branches,
            bot_token_secret=secrets.get("bot_token", "BOT_TOKEN"),
            claude_token_secret=secrets.get("claude_token", "CLAUDE_CODE_OAUTH_TOKEN"),
            model=model,
            setup=setup,
            workflows=workflows,
            allowed_repo_secrets=allowed,
        )
