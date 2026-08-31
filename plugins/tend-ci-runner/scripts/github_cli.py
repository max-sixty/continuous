"""Small typed boundary around the authenticated ``gh`` CLI.

The plugin runs inside both Tend harnesses.  ``gh`` already owns authentication,
proxy routing, and repository selection there, so the Python tools keep it as
their transport and parse the returned JSON themselves.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable
from typing import Any


def run(*args: str, input: str | None = None, quiet: bool = False) -> str:
    """Run ``gh`` and return stdout, raising on a non-zero exit."""
    env = os.environ.copy()
    env.update(NO_COLOR="1", CLICOLOR_FORCE="0")
    result = subprocess.run(
        ["gh", *args],
        input=input,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode:
        if result.stderr and not quiet:
            sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result.stdout


def json_call(*args: str, input: str | None = None, quiet: bool = False) -> Any:
    """Run ``gh`` and parse its one JSON response."""
    return json.loads(run(*args, input=input, quiet=quiet))


def json_stream(*args: str, quiet: bool = False) -> list[Any]:
    """Parse the JSON documents emitted by ``gh api --paginate``."""
    text = run(*args, quiet=quiet)
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        document, position = decoder.raw_decode(text, position)
        documents.append(document)
    return documents


def paginated(*args: str, quiet: bool = False) -> list[Any]:
    """Return every item from a paginated array endpoint in API order."""
    items: list[Any] = []
    for page in json_stream(*args, quiet=quiet):
        if not isinstance(page, list):
            raise ValueError("paginated GitHub response was not an array")
        items.extend(page)
    return items


def repository() -> str:
    """Return the explicitly selected repository or the checkout's remote."""
    selected = os.environ.get("GITHUB_REPOSITORY")
    if selected:
        return selected
    response = json_call("repo", "view", "--json", "nameWithOwner")
    return str(response["nameWithOwner"])


def dump(value: Any) -> None:
    """Write a stable JSON document to stdout."""
    json.dump(
        value,
        sys.stdout,
        indent=2,
        separators=(",", ": "),
        ensure_ascii=False,
    )
    sys.stdout.write("\n")


def actor_login(actor: object) -> str:
    """Return a GitHub actor login, including for deleted-account records."""
    if not isinstance(actor, dict):
        return ""
    return str(actor.get("login") or "")


def unique(values: Iterable[str]) -> list[str]:
    """Deduplicate strings without changing their order."""
    return list(dict.fromkeys(values))


def exit_code(error: subprocess.CalledProcessError) -> int:
    """Map a failed ``gh`` invocation to a useful process exit code."""
    return error.returncode or 1
