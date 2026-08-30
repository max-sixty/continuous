"""Helpers shared by the Python step bodies in this directory.

A step body is a flat module beside this one, run by the composite action as
``/usr/bin/python3 -E -s <action_path>/../shared/steps/<name>.py`` with its
inputs in the environment, exactly as the shell bodies were. Only the standard
library is available: the steps run on the runner's ``/usr/bin/python3``, before
and without tend's own ``uv``. That is 3.12 on the pinned ubuntu-24.04 image,
but an adopter can select an older image through the documented ``runs-on``
override, so these modules stay 3.10-compatible.

Every GitHub call goes through :func:`gh`, and every step module calls it as
``_common.gh(...)`` rather than importing the name, so a test replaces one
attribute (the ``fake_gh`` fixture in ``conftest.py``) instead of standing up a
shim on ``PATH``.

The runner reads ``::``-prefixed workflow commands from the start of any line a
step prints. Text a step did not write itself — the agent's stderr, a comment
body — is printed inside :func:`stop_commands`, which brackets it with a
per-run token so it can neither post annotations nor switch command processing
off for the steps that follow.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def require_env(*names: str) -> dict[str, str]:
    """Return the named variables, failing on any that is unset *or empty*.

    Most step inputs arrive as Actions inputs or step outputs, which are always
    set and can be empty — and an empty ``TEND_SYSTEM_PROMPT`` would launch the
    agent with none of tend's directives and report the run green. Call this at
    the top of ``main`` so a missing input fails there, by name, rather than
    wherever it is first read.
    """
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(f"missing required environment: {', '.join(missing)}")
    return values


def gh(*args: str, input: str | None = None) -> str:
    """Run ``gh`` with ``args`` and return its stdout.

    A non-zero exit raises ``subprocess.CalledProcessError`` with stderr
    attached; a step that can tolerate a particular failure catches that. The
    stderr is also relayed to this process's stderr first, whether or not the
    caller tolerates the failure: "Bad credentials" or "Not Found" is the whole
    diagnosis for a misconfigured install, and the shell bodies had it on the
    step log for free.
    """
    result = subprocess.run(
        ["gh", *args],
        input=input,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.stderr.flush()
        raise subprocess.CalledProcessError(
            result.returncode, ["gh", *args], result.stdout, result.stderr
        )
    return result.stdout


def gh_json(*args: str, input: str | None = None) -> Any:
    """:func:`gh` whose output is parsed as JSON."""
    return json.loads(gh(*args, input=input))


# What a `gh` read can fail with, for a step that tolerates one failing.
# Catching the non-zero exit alone is not enough: a GitHub blip can answer a
# request with an HTML error page under a 200, so `gh` exits zero and the parse
# is what fails. The shell bodies got both for free — they read through
# `gh --jq`, which made `gh` itself fail on an unparsable body, and their
# `|| true` swallowed that too.
GH_READ_FAILED = (subprocess.CalledProcessError, json.JSONDecodeError)


def gh_paginated(path: str) -> list[Any]:
    """Every item a paginated ``gh api`` array endpoint serves, in page order.

    ``--paginate`` alone prints one JSON document per page, which is not a
    document; ``--slurp`` makes it the array of pages, flattened here. It also
    refuses ``--jq``, which is the point — the filtering stays in Python, where
    a test sees the predicate rather than a jq string.
    """
    pages = gh_json("api", "--paginate", "--slurp", path)
    return [item for page in pages for item in page]


# Where each event keeps the number of the issue or PR it is about.
# `repository_dispatch` is tend-mention relaying review events through a
# secretless job that re-posts them, so its PR number arrives in the dispatch
# payload — and as a form field, hence a string.
_SUBJECT_NUMBER_KEYS = {
    "pull_request_target": ("pull_request", "number"),
    "pull_request_review": ("pull_request", "number"),
    "pull_request_review_comment": ("pull_request", "number"),
    "issues": ("issue", "number"),
    "issue_comment": ("issue", "number"),
    "repository_dispatch": ("client_payload", "pr"),
}
# Where each event keeps its own commit. Only the pull-request events and
# `workflow_run` carry one; see :func:`subject_sha` for what the rest report.
_SUBJECT_SHA_KEYS = {
    "pull_request_target": ("pull_request", "head", "sha"),
    "pull_request_review": ("pull_request", "head", "sha"),
    "pull_request_review_comment": ("pull_request", "head", "sha"),
    "workflow_run": ("workflow_run", "head_sha"),
}


def event_payload() -> dict[str, Any]:
    """The triggering event's payload, or ``{}`` for anything unreadable.

    Every caller reads a fact *about* the run — the thread it came from, the
    commit it is on — to annotate work it is doing anyway. So an unreadable
    payload leaves that fact ``None``: a missing file, a body that is not JSON,
    or no ``GITHUB_EVENT_PATH`` at all. Raising instead would lose the whole
    record the fact was going to annotate.
    """
    try:
        payload = json.loads(
            Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, KeyError):
        return {}
    return payload if isinstance(payload, dict) else {}


def dig(payload: Any, *keys: str) -> Any:
    """``payload[k1][k2]…``, or ``None`` for any shape that lacks that path."""
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def as_int(value: Any) -> int | None:
    """``value`` as an ``int``, or ``None`` for anything that isn't a whole one.

    The same number reaches these steps as a JSON int in one event's payload
    and as a string in another's (``repository_dispatch``'s form-encoded
    ``client_payload``), and every ``GITHUB_*`` variable is a string. A
    consumer that groups records by such a number must not see the two forms
    as different keys.

    Bools and floats are refused rather than coerced. Anyone holding
    ``contents: write`` can POST a ``repository_dispatch`` with any JSON type
    in ``client_payload.pr``, and ``_issue.ref()`` renders what comes back
    into a public issue comment: ``true`` coerced to ``1``, or ``3.9`` to
    ``3``, is a plausible-looking reference to somebody else's PR, where
    ``None`` is no reference at all.
    """
    if isinstance(value, (bool, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def subject_number() -> int | None:
    """The issue or PR number the triggering event is about, if it has one.

    ``None`` for an event with no thread of its own (``schedule``,
    ``workflow_dispatch``, ``workflow_run``) and for a payload whose shape is
    not the one its event promises.
    """
    keys = _SUBJECT_NUMBER_KEYS.get(os.environ.get("GITHUB_EVENT_NAME", ""))
    return as_int(dig(event_payload(), *keys)) if keys else None


def subject_sha() -> str | None:
    """The commit the triggering event is about, when its subject is a commit.

    An event's subject is a thread or a commit, and this reports the commit
    half: the payload's where the event names one, and ``GITHUB_SHA`` — the
    ref the run was queued on — for the events that name neither a thread nor
    a commit, ``schedule`` and ``workflow_dispatch``. An event that names a
    thread gets ``None``, because ``GITHUB_SHA`` is the default branch's tip
    there and the run is not about it: a mention on a PR ``gh pr checkout``s
    the PR's head straight after. :func:`subject_number` has that subject.

    ``GITHUB_SHA`` is likewise the wrong commit for the events that do carry
    their own, which is why the payload wins: a ``pull_request_target`` run
    reports the default branch's tip there rather than the PR head the
    workflow checks out, and a ``workflow_run`` run reports neither the
    failing run's commit nor its own.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    keys = _SUBJECT_SHA_KEYS.get(event)
    sha = dig(event_payload(), *keys) if keys else None
    if isinstance(sha, str) and sha:
        return sha
    if event in _SUBJECT_NUMBER_KEYS:
        return None
    return os.environ.get("GITHUB_SHA") or None


def utcnow() -> datetime.datetime:
    """The one clock the steps read, always UTC.

    The shell bodies reached for GNU ``date -u`` for the same reason: every
    timestamp a step writes or compares — a row's stamp, a "today" window, a
    baseline range — is UTC, and a local-time reading would silently shift a
    day boundary the limits are scoped to.
    """
    return datetime.datetime.now(datetime.UTC)


def read_ndjson(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects in an NDJSON file, skipping lines that aren't one.

    The stream-json the agent writes can end in a partial line when the process
    is killed mid-write; the events before it are still the run's record.
    """
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def annotate(level: str, message: str) -> None:
    """Emit a workflow annotation (``error``, ``warning`` or ``notice``).

    ``message`` is one line: a break would end the annotation and start a new
    line the runner parses on its own. Flattened with ``splitlines`` rather
    than on ``\\n`` alone, because the runner ends a line on a bare ``\\r``
    too and some of what lands here — an agent-written failure reason — is
    text tend did not compose.
    """
    print(f"::{level}::{' '.join(message.splitlines())}", flush=True)


@contextlib.contextmanager
def stop_commands() -> Iterator[None]:
    """Print untrusted text inside this block so it cannot issue workflow commands."""
    token = f"tend-{secrets.token_hex(8)}"
    print(f"::stop-commands::{token}", flush=True)
    try:
        yield
    finally:
        print(f"::{token}::", flush=True)


def _append(env_name: str, text: str) -> None:
    # `errors="replace"` for the same reason `run` reconfigures the streams: a
    # lone surrogate from a `\udXXX` escape in agent-written text is not
    # encodable, and losing the whole summary or output to one is worse than
    # losing the character.
    with Path(os.environ[env_name]).open(
        "a", encoding="utf-8", errors="replace"
    ) as handle:
        handle.write(text)


def set_output(key: str, value: str) -> None:
    """Publish a step output. Multi-line values use the heredoc form."""
    if "\n" in value:
        delimiter = f"tend-{secrets.token_hex(8)}"
        _append("GITHUB_OUTPUT", f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    else:
        _append("GITHUB_OUTPUT", f"{key}={value}\n")


def set_env(key: str, value: str) -> None:
    """Export a variable to every later step in the job."""
    if "\n" in value:
        delimiter = f"tend-{secrets.token_hex(8)}"
        _append("GITHUB_ENV", f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    else:
        _append("GITHUB_ENV", f"{key}={value}\n")


def append_summary(markdown: str) -> None:
    """Append to the job summary; a trailing newline is added when missing."""
    _append(
        "GITHUB_STEP_SUMMARY", markdown if markdown.endswith("\n") else markdown + "\n"
    )


def log(step: str, message: str) -> None:
    """One-line progress note in the step log, prefixed with the step's name."""
    print(f"[{step}] {message}", flush=True)


def fail(message: str, code: int = 1) -> int:
    """Annotate an error and return the exit code for ``main`` to hand back."""
    annotate("error", message)
    return code


def run(main: Any) -> None:
    """``if __name__ == "__main__"`` boilerplate: exit with ``main()``'s code.

    A ``gh`` call the step could not proceed without surfaces as one
    ``::error::`` naming the command, with ``gh``'s own explanation already on
    stderr from :func:`gh`; a traceback would say less and bury it.

    The streams take ``errors="replace"`` first. Agent-written text reaches
    them — a transcript, a quoted stderr tail, a failure reason — and a
    ``\\udXXX`` escape in the agent's JSON decodes to a lone surrogate that
    UTF-8 cannot encode. Unreplaced it raises ``UnicodeEncodeError`` from the
    print itself, reddening a run that otherwise succeeded.
    """
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as err:
        cmd = " ".join(str(part) for part in err.cmd)
        sys.exit(fail(f"{cmd} failed (exit {err.returncode})"))
