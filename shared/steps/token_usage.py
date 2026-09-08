"""Accounts a run's token usage and publishes it three ways.

Run as ``token_usage.py --harness {claude,codex}`` from the "Token usage" step
of either composite action. That step is ``if: always()`` — a cancelled or
failed run still has to report what it spent — so nothing here may fail the
job: every copy is best-effort and every parse tolerates a torn line.

Reads (env):
  MODEL             - model name, copied through to the record; may be empty
  STREAM_JSON       - claude: the headless run's stream-json (NDJSON of SDK
                      message events); may be empty or name a missing file
  AGENT_HOME        - the sandbox user's home, exported by
                      setup_sandbox.py via ``$GITHUB_ENV``. Unset when setup
                      died early, which is why consolidation tolerates it
  RUNNER_TEMP       - parent of the consolidated log dir, and where Claude's
                      stderr log was written
  SANDBOX_REAPED    - ``true`` after the harness has stopped every sandbox
                      process; agent-owned session trees are copied only then
  GITHUB_REPOSITORY - the record's ``repo``; on claude it also gates the raw
                      stream-json copy to tend's own repo
  GITHUB_WORKFLOW, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_EVENT_NAME,
  GITHUB_SHA, GITHUB_EVENT_PATH
                    - the rest of :func:`run_context`

Writes ``token-usage.json`` into the consolidated log dir (uploaded as the
session-log artifact), the ``usage`` step output (compact JSON), and a
``## Token Usage`` table in the job summary. The record's shape mirrors the
interactive harness so downstream consumers (review-reviewers' evidence gist,
token_report.py, dashboards) don't branch on harness.

Every record also names the run it came from, so spend can be grouped by
subject; see :func:`run_context`. The job summary stays counts-only, because
the run page it is rendered on already names the run.

Claude's accounting has three paths, tried in order:

1. The stream-json's ``type: "result"`` events. Sessions that use
   ``run_in_background: true`` Bash emit a second ``result`` on wakeup;
   ``usage.*`` and ``num_turns`` are per-event while ``total_cost_usd`` is
   cumulative, so the per-event fields are summed and cost taken from the last.
2. No result event: the session JSONL this step has just consolidated. A
   cancelled session never emits a result, and ``tend-triage`` runs with
   ``cancel-in-progress: true``, so this is routine rather than exotic — the
   run may have done dozens of turns and already posted its comment.
3. Neither: the agent never ran (a preflight failure, say). That run genuinely
   cost nothing, so it reports a real zero rather than flagging an unknown.

Path 2 reads the session JSONL, NOT the stream-json, even though both carry
``type: "assistant"`` events. The stream's are non-final (``stop_reason:
null``): ``usage.output_tokens`` is the message-start placeholder — single
digits against thousands — while the input and cache fields, known at message
start, do match. Summing the stream's events would under-count output by
orders of magnitude and look plausible doing it.

Both files record each assistant message roughly twice, hence the dedupe by
``message.id``. ``<session-id>/subagents/agent-*.jsonl`` is skipped: every
``Task`` subagent gets its own transcript there and the bounded tree export
brings the subtree along, but the ``result`` event path
2 stands in for counts only the main loop — slurping the subagents alongside it
inflates every field (turns roughly doubles) and makes partial runs
incomparable with complete ones.

``partial: true`` marks path 2, and its ``cost_usd`` is null because only
``result.total_cost_usd`` carries cost and that is the event this path does not
have. A ``0`` there would repeat the bug the fallback exists to fix, one field
down; ``partial`` is what keeps a reconstructed total distinguishable from a
run that really cost nothing.

Codex has one path: consolidate the sandbox user's rollouts, then sum
``sessions/**/rollout-*.jsonl``. That schema isn't versioned, so a
missing field counts as zero — and no rollouts at all is the same computation
over no events, which yields the all-zero record on its own. Cost stays 0: the
Codex CLI doesn't surface API list prices and computing them here would mean
maintaining a price table.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import _common
from _safe_files import open_directory_nofollow

# Row order is the rendered table's; the two harnesses report different metrics
# under the same heading, so neither list is a subset of the other.
CLAUDE_TABLE = (
    "## Token Usage",
    (
        ("Input", "input_tokens"),
        ("Output", "output_tokens"),
        ("Cache creation", "cache_creation_input_tokens"),
        ("Cache read", "cache_read_input_tokens"),
        ("Cost", "cost_usd"),
        ("Turns", "turns"),
    ),
)
CODEX_TABLE = (
    "## Token Usage (Codex)",
    (
        ("Input", "input_tokens"),
        ("Output", "output_tokens"),
        ("Cached input", "cached_input_tokens"),
        ("Turns", "turns"),
        ("Model", "model"),
    ),
)

PARTIAL_NOTE = (
    "*Reconstructed from the session log: this run emitted no result event "
    "(most often a cancellation), so the token counts are its own but the cost "
    "is not recoverable.*"
)
LIST_PRICE_NOTE = (
    "*Cost at API list prices — a large multiple of the effective rate on "
    "Claude Code subscriptions.*"
)
CODEX_COST_NOTE = "*Cost is not reported — Codex CLI does not surface API list prices.*"
EXPORT_MAX_FILES = 20_000
EXPORT_MAX_FILE_BYTES = 64 * 1024 * 1024
EXPORT_MAX_TOTAL_BYTES = 512 * 1024 * 1024


def main() -> int:
    if len(sys.argv) == 6 and sys.argv[1] == "--copy-tree":
        if os.geteuid() != 0:
            raise SystemExit("--copy-tree requires root")
        return privileged_copy(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            uid=int(sys.argv[4]),
            gid=int(sys.argv[5]),
        )
    parser = argparse.ArgumentParser(description="Account a run's token usage.")
    parser.add_argument("--harness", choices=("claude", "codex"), required=True)
    harness = parser.parse_args().harness

    model = os.environ.get("MODEL", "")
    if harness == "claude":
        usage, logs_dir = claude_step(model)
    else:
        usage, logs_dir = codex_step(model)

    record = {**run_context(), **usage}
    payload = json.dumps(record, separators=(",", ":"))
    (logs_dir / "token-usage.json").write_text(payload + "\n", encoding="utf-8")
    _common.set_output("usage", payload)
    _common.append_summary(render_summary(usage, harness=harness))
    return 0


def privileged_copy(source: Path, destination: Path, *, uid: int, gid: int) -> int:
    """Descriptor-relative, bounded copy used only through the root helper."""
    source_fd = open_directory_nofollow(source)
    destination.mkdir(parents=True, exist_ok=True)
    destination_fd = open_directory_nofollow(destination)
    os.fchown(destination_fd, uid, gid)
    files = 0
    total = 0

    def copy_dir(src_fd: int, dst_fd: int) -> None:
        nonlocal files, total
        with os.scandir(src_fd) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    os.mkdir(entry.name, mode=0o700, dir_fd=dst_fd)
                    os.chown(
                        entry.name,
                        uid,
                        gid,
                        dir_fd=dst_fd,
                        follow_symlinks=False,
                    )
                    child_src = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=src_fd,
                    )
                    child_dst = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=dst_fd,
                    )
                    try:
                        copy_dir(child_src, child_dst)
                    finally:
                        os.close(child_src)
                        os.close(child_dst)
                    continue
                if not stat.S_ISREG(mode):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
                files += 1
                total += size
                if files > EXPORT_MAX_FILES:
                    raise ValueError("session export exceeded file-count limit")
                if size > EXPORT_MAX_FILE_BYTES or total > EXPORT_MAX_TOTAL_BYTES:
                    raise ValueError("session export exceeded byte limit")
                src = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd)
                try:
                    if not stat.S_ISREG(os.fstat(src).st_mode):
                        continue
                    dst = os.open(
                        entry.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=dst_fd,
                    )
                    try:
                        os.fchown(dst, uid, gid)
                        remaining = size
                        while remaining:
                            chunk = os.read(src, min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            view = memoryview(chunk)
                            while view:
                                view = view[os.write(dst, view) :]
                            remaining -= len(chunk)
                    finally:
                        os.close(dst)
                finally:
                    os.close(src)

    try:
        copy_dir(source_fd, destination_fd)
    finally:
        os.close(source_fd)
        os.close(destination_fd)

    return 0


def run_context() -> dict[str, Any]:
    """The run's identity, so a record says what the spend went to.

    Without it a record answers "how much?" and nothing else, and the reader
    is left joining every artifact back to a run listing to find out what it
    was working on. Two of these keys are not on that listing at all: the
    PR/issue ``number``, and a ``head_sha`` read from the event rather than
    from the ref the run was queued on. The rest are, and they are here so the
    record stands on its own — read out of a gist or a downloaded artifact,
    away from the run that wrote it.

    Read from the Actions environment, which is always set in a job, and from
    the event payload, which may not be readable. Each key resolves on its
    own, so a surprising event shape costs that key rather than the record.
    """
    env = os.environ
    return {
        "repo": env.get("GITHUB_REPOSITORY") or None,
        "workflow": env.get("GITHUB_WORKFLOW") or None,
        "run_id": _common.as_int(env.get("GITHUB_RUN_ID")),
        "run_attempt": _common.as_int(env.get("GITHUB_RUN_ATTEMPT")),
        "event": env.get("GITHUB_EVENT_NAME") or None,
        "number": _common.subject_number(),
        "head_sha": _common.subject_sha(),
    }


def claude_step(model: str) -> tuple[dict[str, Any], Path]:
    """Consolidate the sandbox's logs, then account the run from them."""
    runner_temp = Path(_common.require_env("RUNNER_TEMP")["RUNNER_TEMP"])
    logs_dir = runner_temp / "tend-logs"
    consolidate_logs(logs_dir, runner_temp)

    stream_json = os.environ.get("STREAM_JSON", "")
    stream = Path(stream_json) if stream_json else None
    usage = claude_usage(stream=stream, logs_dir=logs_dir, model=model)

    # Preserve the raw stream-json (the only place `type: "result"` cost events
    # live) alongside the session JSONL so token under-reporting (#302) can be
    # diagnosed against an actual stream. Gated to tend's own repo so consumers
    # don't pay for an upload that only serves a tend-internal diagnostic. Drop
    # once #302 is resolved.
    if (
        os.environ.get("GITHUB_REPOSITORY") == "max-sixty/tend"
        and stream is not None
        and stream.is_file()
    ):
        best_effort("cp", str(stream), str(logs_dir / "claude-stream.json"))
    return usage, logs_dir


def codex_step(model: str) -> tuple[dict[str, Any], Path]:
    runner_temp = Path(_common.require_env("RUNNER_TEMP")["RUNNER_TEMP"])
    logs_dir = runner_temp / "tend-codex-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    agent_home = Path(os.environ.get("AGENT_HOME") or "/nonexistent")
    if os.environ.get("SANDBOX_REAPED") == "true":
        copy_agent_tree(agent_home / ".codex" / "sessions", logs_dir / "sessions")
    rollouts = sorted((logs_dir / "sessions").rglob("rollout-*.jsonl"))
    return codex_usage(read_all(rollouts), model), logs_dir


def consolidate_logs(logs_dir: Path, runner_temp: Path) -> None:
    """Copy the agent's session JSONL and stderr log into a runner-owned dir.

    The copy waits on ``SANDBOX_REAPED``, the supervisor's record that every
    sandbox process is dead. :func:`copy_agent_tree` then walks through
    no-follow descriptors, so path checks and reads are one operation rather
    than a check followed by a privileged pathname traversal.

    It is set from a ``finally``, so only a supervisor killed outright — a
    second signal during an escalating cancel — leaves it unset. That run
    reports its usage from the stream-json and uploads no session logs.

    ``AGENT_HOME`` is unset when sandbox setup died before exporting it; the
    placeholder names nothing, which :func:`copy_agent_tree` reads as the same
    nothing-to-copy outcome as a run whose agent never started.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    agent_home = Path(os.environ.get("AGENT_HOME") or "/nonexistent")
    if os.environ.get("SANDBOX_REAPED") == "true":
        copy_agent_tree(agent_home / ".claude" / "projects", logs_dir)
    best_effort("cp", "-a", str(runner_temp / "tend-claude-stderr.log"), f"{logs_dir}/")


def copy_agent_tree(source: Path, destination: Path) -> None:
    """Copy a quiescent agent tree through one bounded root helper.

    The helper opens every directory and file relative to an already-open
    descriptor with ``O_NOFOLLOW``. Symlinks, devices and FIFOs are skipped;
    file count, per-file bytes and total bytes are bounded before upload.
    """
    destination.mkdir(parents=True, exist_ok=True)
    best_effort(
        "/usr/bin/sudo",
        "-n",
        "/usr/bin/python3",
        "-E",
        "-s",
        str(Path(__file__).resolve()),
        "--copy-tree",
        str(source),
        str(destination),
        str(os.getuid()),
        str(os.getgid()),
    )


def best_effort(*argv: str) -> None:
    """Run ``argv``, discarding its output and its failure.

    Everything this runs is a copy, a chown, or a chmod that enriches the
    uploaded artifact. None of it is worth failing an ``if: always()`` step
    for, and a partial copy still beats no artifact. ``stdin`` is closed so a
    ``sudo`` without a tty fails instead of waiting for a password.
    """
    subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def claude_usage(*, stream: Path | None, logs_dir: Path, model: str) -> dict[str, Any]:
    """The run's record, from the result events, the session JSONL, or zero."""
    if stream is not None and stream.is_file() and stream.stat().st_size > 0:
        usage = result_usage(_common.read_ndjson(stream), model)
        if usage is not None:
            return usage

    sessions = session_files(logs_dir)
    if sessions:
        usage = session_usage(read_all(sessions), len(sessions), model)
        if usage is not None:
            return usage

    return claude_record(
        lambda _field: 0, turns=0, model=model, cost_usd=0, partial=False
    )


def session_files(logs_dir: Path) -> list[Path]:
    """The consolidated session JSONLs, without the `Task` subagent transcripts."""
    if not logs_dir.is_dir():
        return []
    return sorted(
        path
        for path in logs_dir.rglob("*.jsonl")
        if path.is_file() and "subagents" not in path.relative_to(logs_dir).parts
    )


def read_all(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Every JSON object across ``paths``, each file read on its own.

    Per-file reads are what keep a truncated tail costing only its own line: a
    parser that concatenated the files first would glue a file's partial last
    line to the next file's first line and lose both.
    """
    for path in paths:
        yield from _common.read_ndjson(path)


def claude_record(
    total: Callable[[str], float],
    *,
    turns: int,
    model: str,
    cost_usd: float | None,
    partial: bool,
) -> dict[str, Any]:
    """The record's eight keys, whichever path accounted the run.

    The three Claude paths differ only in where their numbers come from, so
    ``total`` takes a usage field's name and returns that path's sum for it.
    Downstream consumers then read the same eight keys from all three.
    """
    return {
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cache_creation_input_tokens": total("cache_creation_input_tokens"),
        "cache_read_input_tokens": total("cache_read_input_tokens"),
        "turns": turns,
        "model": model,
        "cost_usd": cost_usd,
        "partial": partial,
    }


def result_usage(events: Iterable[dict[str, Any]], model: str) -> dict[str, Any] | None:
    """Account from the stream-json's ``result`` events, or None if it has none."""
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        return None

    def total(field: str) -> float:
        return sum(number(event.get("usage"), field) for event in results)

    return claude_record(
        total,
        turns=sum(number(event, "num_turns") for event in results),
        model=model,
        cost_usd=round(number(results[-1], "total_cost_usd"), 2),
        partial=False,
    )


def session_usage(
    events: Iterable[dict[str, Any]], sessions: int, model: str
) -> dict[str, Any] | None:
    """Reconstruct from the session JSONLs, or None if they hold no assistant message.

    ``sessions`` is how many files the events were pooled from: the prompt that
    opens a session is a ``user`` line but not a turn, so one is subtracted per
    session rather than one overall.
    """
    messages: dict[str, dict[str, Any]] = {}
    user_lines = 0
    for event in events:
        if event.get("type") == "user":
            user_lines += 1
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if isinstance(message, dict) and message.get("id") is not None:
            messages.setdefault(message["id"], message)

    if not messages:
        return None

    def total(field: str) -> float:
        return sum(number(message.get("usage"), field) for message in messages.values())

    return claude_record(
        total,
        turns=max(user_lines - sessions, 0),
        model=model,
        cost_usd=None,
        partial=True,
    )


def codex_usage(events: Iterable[dict[str, Any]], model: str) -> dict[str, Any]:
    """Sum a Codex run's rollout events; no events is the all-zero record."""
    events = list(events)
    if not model:
        for event in events:
            payload = event.get("payload")
            if event.get("type") != "turn_context" or not isinstance(payload, dict):
                continue
            value = payload.get("model")
            if isinstance(value, str) and value:
                model = value
                break

    def total(field: str) -> float:
        return sum(number(event.get("token_count"), field) for event in events)

    return {
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cached_input_tokens": total("cached_input_tokens"),
        "turns": sum(1 for event in events if event.get("type") == "agent_message"),
        "model": model,
        "cost_usd": 0,
    }


def number(container: Any, field: str) -> float:
    """``container[field]`` when it is a number, else 0.

    Neither harness versions the events read here, so a field that is absent,
    null, or the wrong type costs its own contribution rather than the run's
    whole accounting.
    """
    value = container.get(field) if isinstance(container, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def render_summary(usage: dict[str, Any], *, harness: str) -> str:
    """The job-summary section: one table plus the harness's footnotes."""
    title, rows = CLAUDE_TABLE if harness == "claude" else CODEX_TABLE
    lines = [title, "| Metric | Value |", "|--------|-------|"]
    lines += [f"| {label} | {cell(usage[key], key)} |" for label, key in rows]
    lines.append("")
    if harness == "claude":
        if usage["partial"]:
            lines.append(PARTIAL_NOTE)
        lines.append(LIST_PRICE_NOTE)
    else:
        lines.append(CODEX_COST_NOTE)
    return "\n".join(lines) + "\n"


def cell(value: Any, key: str) -> str:
    """A table cell: cost as dollars, everything else as it stands."""
    if key != "cost_usd":
        return str(value)
    return "unknown" if value is None else f"${value:.2f}"


if __name__ == "__main__":
    _common.run(main)
