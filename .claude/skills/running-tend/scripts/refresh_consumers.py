# /// script
# requires-python = ">=3.12"
# dependencies = ["ruamel.yaml>=0.18"]
# ///
"""Refresh Tend's public consumer index from generated workflow installations.

GitHub code search discovers new installations but has incomplete recall, so
the command unions search results with the current index. A successful refresh
retains only repositories whose default branch contains a generated Tend
workflow and a config with a bot name. All GitHub reads finish before the index
is replaced; an API failure therefore leaves the existing file intact.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

CONSUMERS_PATH = Path("data/consumers.json")
WORKFLOW_PREFIX = ".github/workflows/tend-"


def github_json(*args: str, allow_missing: bool = False) -> Any:
    """Run an authenticated GitHub CLI read and parse its JSON response."""
    env = os.environ.copy()
    env.update(NO_COLOR="1", CLICOLOR_FORCE="0")
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode:
        if allow_missing and "HTTP 404" in result.stderr:
            return None
        if result.stderr:
            sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return json.loads(result.stdout)


def load_consumers(path: Path) -> list[dict[str, str]]:
    """Read and validate the existing consumer index."""
    if not path.exists():
        return []
    value = json.loads(path.read_text())
    if not isinstance(value, list):
        raise TypeError(f"{path} must contain a JSON array")

    consumers: list[dict[str, str]] = []
    for entry in value:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("repo"), str)
            or not isinstance(entry.get("bot_name"), str)
        ):
            raise TypeError(f"{path} contains an invalid consumer entry")
        consumers.append({"repo": entry["repo"], "bot_name": entry["bot_name"]})
    return consumers


def discover_repositories() -> set[str]:
    """Return repositories found through generated Tend workflow files."""
    results = github_json(
        "search",
        "code",
        "max-sixty/tend",
        "--extension",
        "yaml",
        "--limit",
        "100",
        "--json",
        "repository,path",
    )
    if not isinstance(results, list):
        raise TypeError("GitHub code search response was not an array")

    repositories: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise TypeError("GitHub code search returned an invalid result")
        path = result.get("path")
        repository = result.get("repository")
        if not isinstance(path, str) or not isinstance(repository, dict):
            raise TypeError("GitHub code search returned an invalid result")
        name = repository.get("nameWithOwner")
        if not isinstance(name, str):
            raise TypeError("GitHub code search returned an invalid repository")
        if "/" not in name:
            raise ValueError("GitHub code search returned an invalid repository")
        if path.startswith(WORKFLOW_PREFIX):
            repositories.add(name)
    return repositories


def read_consumer(repository: str) -> dict[str, str] | None:
    """Return one installed consumer, or ``None`` when no Tend workflow remains."""
    workflows = github_json(
        "api",
        f"repos/{repository}/contents/.github/workflows",
        allow_missing=True,
    )
    if workflows is None:
        return None
    if not isinstance(workflows, list):
        raise TypeError(f"workflow listing for {repository} was not an array")
    if not any(
        isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"].startswith("tend-")
        for entry in workflows
    ):
        return None

    config = github_json(
        "api",
        f"repos/{repository}/contents/.config/tend.yaml",
        allow_missing=True,
    )
    if config is None:
        return None
    if not isinstance(config, dict) or not isinstance(config.get("content"), str):
        raise TypeError(f"Tend config response for {repository} was invalid")

    encoded = "".join(config["content"].splitlines())
    parsed = YAML(typ="safe").load(base64.b64decode(encoded, validate=True).decode())
    if not isinstance(parsed, dict) or not isinstance(parsed.get("bot_name"), str):
        raise TypeError(f"Tend config for {repository} has no bot_name")
    if not parsed["bot_name"]:
        raise ValueError(f"Tend config for {repository} has an empty bot_name")
    return {"repo": repository, "bot_name": parsed["bot_name"]}


def refresh(path: Path = CONSUMERS_PATH) -> dict[str, object]:
    """Refresh *path* and return a summary for the weekly agent."""
    existing = load_consumers(path)
    existing_repositories = {entry["repo"] for entry in existing}
    discovered = discover_repositories()

    consumers = [
        consumer
        for repository in sorted(existing_repositories | discovered)
        if (consumer := read_consumer(repository)) is not None
    ]
    rendered = json.dumps(consumers, indent=2, ensure_ascii=False) + "\n"
    changed = not path.exists() or path.read_text() != rendered
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)

    kept = {entry["repo"] for entry in consumers}
    return {
        "changed": changed,
        "discovered": len(discovered),
        "consumers": len(consumers),
        "removed": sorted(existing_repositories - kept),
    }


def main(argv: list[str] | None = None) -> int:
    """Run the no-argument updater."""
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("usage: refresh_consumers.py", file=sys.stderr)
        return 2
    try:
        summary = refresh()
    except subprocess.CalledProcessError as error:
        return error.returncode or 1
    except (OSError, TypeError, ValueError, YAMLError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
