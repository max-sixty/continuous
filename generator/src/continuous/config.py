"""Read and validate .config/continuous.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import click


@dataclass
class SetupStep:
    """A single project setup step — either a `uses:` action or a `run:` command."""

    uses: str = ""
    run: str = ""


@dataclass
class WorkflowConfig:
    enabled: bool = True
    prompt: str = ""
    cron: str = ""
    watched_workflows: list[str] = field(default_factory=list)


@dataclass
class Config:
    bot_name: str
    bot_token_secret: str
    claude_token_secret: str
    setup: list[SetupStep]
    workflows: dict[str, WorkflowConfig]

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        if path is None:
            path = Path(".config/continuous.toml")
        if not path.exists():
            raise click.ClickException(f"Config not found: {path}")
        with path.open("rb") as f:
            raw = tomllib.load(f)

        secrets = raw.get("secrets", {})

        setup: list[SetupStep] = []
        setup_raw = raw.get("setup", {})
        for action in setup_raw.get("uses", []):
            setup.append(SetupStep(uses=action))
        for cmd in setup_raw.get("run", []):
            setup.append(SetupStep(run=cmd))

        workflows: dict[str, WorkflowConfig] = {}
        for name, wf_raw in raw.get("workflows", {}).items():
            if isinstance(wf_raw, dict):
                workflows[name] = WorkflowConfig(
                    enabled=wf_raw.get("enabled", True),
                    prompt=wf_raw.get("prompt", ""),
                    cron=wf_raw.get("cron", ""),
                    watched_workflows=wf_raw.get("watched_workflows", []),
                )
            else:
                workflows[name] = WorkflowConfig(enabled=bool(wf_raw))

        return cls(
            bot_name=raw["bot_name"],
            bot_token_secret=secrets.get("bot_token", "BOT_TOKEN"),
            claude_token_secret=secrets.get("claude_token", "CLAUDE_CODE_OAUTH_TOKEN"),
            setup=setup,
            workflows=workflows,
        )
