"""Test doubles for the step-body tests, imported by ``conftest.py``.

A separate module so tests can import the types by a basename that is unique
across the repo: pytest's prepend import mode names non-package test dirs'
modules by basename, and ``from conftest import …`` resolves to whichever
directory's ``conftest.py`` was collected first (``proxy/`` has one too).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Responder = Callable[[tuple[str, ...], str | None], str]


@dataclass
class FakeGh:
    """Serves canned ``gh`` responses and records every call.

    ``respond`` maps an argv prefix (a tuple) to a response: a string is
    returned as stdout, any other object is JSON-encoded first, an ``int``
    raises ``CalledProcessError`` with that exit code, and a callable receives
    ``(argv, stdin)`` and returns the stdout itself. The longest matching
    prefix wins. An argv with no match raises, so a test names every call the
    step makes.
    """

    responses: dict[tuple[str, ...], Any] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    stdins: list[str | None] = field(default_factory=list)

    def respond(self, *argv_prefix: str, with_: Any) -> None:
        self.responses[argv_prefix] = with_

    def __call__(self, *args: str, input: str | None = None) -> str:
        self.calls.append(args)
        self.stdins.append(input)
        for length in range(len(args), 0, -1):
            hit = self.responses.get(args[:length])
            if hit is None:
                continue
            if isinstance(hit, int) and not isinstance(hit, bool):
                raise subprocess.CalledProcessError(
                    hit, ["gh", *args], "", "fake gh failure"
                )
            if callable(hit):
                return hit(args, input)
            if isinstance(hit, str):
                return hit
            return json.dumps(hit)
        raise AssertionError(f"unexpected gh call: {' '.join(args)}")

    def called(self, *argv_prefix: str) -> list[tuple[str, ...]]:
        """The recorded calls that start with ``argv_prefix``."""
        return [c for c in self.calls if c[: len(argv_prefix)] == argv_prefix]


@dataclass
class GithubFiles:
    output: Path
    env: Path
    summary: Path

    def outputs(self) -> dict[str, str]:
        """The ``key=value`` and heredoc entries written so far."""
        result: dict[str, str] = {}
        lines = self.output.read_text().splitlines() if self.output.exists() else []
        i = 0
        while i < len(lines):
            key, sep, rest = lines[i].partition("<<")
            if sep:
                end = lines.index(rest, i + 1)
                result[key] = "\n".join(lines[i + 1 : end])
                i = end + 1
            else:
                key, _, value = lines[i].partition("=")
                result[key] = value
                i += 1
        return result
