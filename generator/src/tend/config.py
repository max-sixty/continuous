"""Read and validate .config/tend.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import click

KNOWN_WORKFLOWS = {"review", "mention", "triage", "ci-fix", "nightly", "renovate"}
KNOWN_TOP_LEVEL = {"bot_name", "secrets", "setup", "workflows"}
_GITHUB_USERNAME = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")


@dataclass
class SetupStep:
    """A single project setup step — `uses:`, `run:`, or `raw:` YAML."""

    uses: str = ""
    run: str = ""
    raw: str = ""


@dataclass
class WorkflowConfig:
    enabled: bool = True
    prompt: str = ""
    cron: str = ""
    watched_workflows: list[str] | None = None
    branches: list[str] | None = None


@dataclass
class Config:
    bot_name: str
    default_branch: str
    bot_token_secret: str
    claude_token_secret: str
    setup: list[SetupStep]
    workflows: dict[str, WorkflowConfig]

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

        unknown = set(raw.keys()) - KNOWN_TOP_LEVEL
        for key in sorted(unknown):
            click.echo(f"Warning: unknown config key '{key}'", err=True)

        secrets = raw.get("secrets", {})

        setup: list[SetupStep] = []
        for i, entry in enumerate(raw.get("setup", [])):
            if not isinstance(entry, dict):
                raise click.ClickException(
                    f"setup[{i}] must be a table with 'uses', 'run', or 'raw'"
                )
            keys = {"uses", "run", "raw"} & entry.keys()
            if len(keys) != 1:
                raise click.ClickException(
                    f"setup[{i}] must have exactly one of 'uses', 'run', or 'raw'"
                )
            key = keys.pop()
            setup.append(SetupStep(**{key: entry[key]}))

        workflows: dict[str, WorkflowConfig] = {}
        for name, wf_raw in raw.get("workflows", {}).items():
            if name not in KNOWN_WORKFLOWS:
                click.echo(f"Warning: unknown workflow '{name}' in config (known: {', '.join(sorted(KNOWN_WORKFLOWS))})", err=True)
            if isinstance(wf_raw, dict):
                watched = wf_raw.get("watched_workflows")
                if watched is not None and len(watched) == 0 and name == "ci-fix":
                    raise click.ClickException(
                        "watched_workflows = [] is invalid for ci-fix — "
                        "workflow_run requires at least one workflow name. "
                        "Disable ci-fix with enabled = false instead."
                    )
                workflows[name] = WorkflowConfig(
                    enabled=wf_raw.get("enabled", True),
                    prompt=wf_raw.get("prompt", ""),
                    cron=wf_raw.get("cron", ""),
                    watched_workflows=watched,
                    branches=wf_raw.get("branches"),
                )
            else:
                workflows[name] = WorkflowConfig(enabled=bool(wf_raw))

        return cls(
            bot_name=bot_name,
            default_branch="main",
            bot_token_secret=secrets.get("bot_token", "BOT_TOKEN"),
            claude_token_secret=secrets.get("claude_token", "CLAUDE_CODE_OAUTH_TOKEN"),
            setup=setup,
            workflows=workflows,
        )
