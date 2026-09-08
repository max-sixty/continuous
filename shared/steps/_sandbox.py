"""Compose the environment at the single runner-to-SRT boundary.

The outer supervisor crosses the UID boundary once with a clean ``env -i``.
SRT then finalizes proxy variables for its network namespace, and every command
inside the lifecycle inherits that environment unchanged. Trusted preparation
commands that run before SRT use :func:`agent_env` and receive no GitHub
context.

Pass ``GITHUB_*`` through as a denylist rather than an explicit allowlist: most
``GITHUB_*`` vars are informational (``GITHUB_ACTOR``, ``GITHUB_API_URL``,
``GITHUB_REF_NAME``, ``GITHUB_WORKSPACE``, …) and a denylist picks up future
additions automatically. Skills depend on them for run-self-reference (branch
names, gist headings, dedup of own check runs) and owner-correct URL
construction. Apart from :data:`WITHHELD`, every ``GITHUB_*`` Actions defines is
public rather than a secret; a ``GITHUB_*``-named variable an adopter's
``setup:`` step writes to ``$GITHUB_ENV`` crosses on the same rule, so a secret
must not be given a ``GITHUB_*`` name.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The GitHub context names that must not cross the uid boundary.
#:
#: ``GITHUB_TOKEN`` — the agent env file carries a dummy; the real PAT lives in
#: the proxy. (The file's dummy must not be overridden, so this entry is
#: load-bearing.)
#:
#: ``GITHUB_WORKSPACE`` — the context names the trusted Actions checkout; the
#: agent env file supplies the disposable clone instead.
#:
#: ``GITHUB_{ENV,PATH,OUTPUT,STATE,STEP_SUMMARY}`` — paths the runner re-reads
#: after the step exits; the sandbox must not be handed a channel into later
#: steps' env / PATH / outputs / job summary.
WITHHELD = frozenset(
    {
        "GITHUB_TOKEN",
        "GITHUB_WORKSPACE",
        "GITHUB_ENV",
        "GITHUB_PATH",
        "GITHUB_OUTPUT",
        "GITHUB_STATE",
        "GITHUB_STEP_SUMMARY",
    }
)


def agent_env(agent_env_file: str | os.PathLike[str]) -> list[str]:
    """Read the fixed sandbox environment as ``NAME=VALUE`` arguments."""
    # Split on newlines alone: a carried value may hold a character `str`
    # considers a line break (\v, \f, U+2028) and the file does not.
    # `newline=""` for the same reason, one layer down: the default translates
    # a lone \r to \n before anything here sees it, so a \r in a `sandbox_env:`
    # value — which the config layer does not reject — would split one
    # NAME=VALUE into two `env` arguments, and `env` execs a trailing argument
    # that is not an assignment as the command to run.
    # `surrogateescape` because the shell that wrote the file was byte
    # transparent: a non-UTF-8 byte in an adopter's `sandbox_env:` value must
    # reach the sandbox as it was written, not fail the step before the launch.
    # `subprocess` re-encodes it with `os.fsencode`, which round-trips it back.
    with Path(agent_env_file).open(
        encoding="utf-8", errors="surrogateescape", newline=""
    ) as handle:
        lines = handle.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def launch_env(agent_env_file: str | os.PathLike[str]) -> list[str]:
    """The ``NAME=VALUE`` arguments for the outer SRT launch.

    The file's lines (proxy routing, CA trust, dummy credentials, and the
    adopter's own ``sandbox_env:`` additions) come first, then the GitHub
    context.

    That order is the reason this composes both halves rather than handing back
    the context alone. ``env`` takes the final assignment of a name, and the
    file is the half an adopter writes, so the context has to follow it or a
    ``sandbox_env: {GITHUB_WORKFLOW: …}`` would decide what the run thinks it
    is. As two lists that was a rule each caller had to remember; here it is the
    function's postcondition. A caller may append names of its own afterwards —
    they win, which is what tend's own ``BOT_*``/``TEND_*`` assignments want,
    since those have to beat the file — provided none is ``GITHUB_*``-named or a
    key the file defines, which would put the context or the sandbox's routing
    back in play.

    Reads the environment when called, so call it in the step that forwards it.
    """
    return agent_env(agent_env_file) + [
        f"{name}={value}"
        for name, value in os.environ.items()
        if name.startswith("GITHUB_") and name not in WITHHELD
    ]
