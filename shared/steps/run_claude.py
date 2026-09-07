"""Runs the agent and decides the step's verdict.

Composes the agent's settings and launch env, starts it as the non-sudo sandbox
user, supervises it to exit or timeout, then turns the finished stream-json into
the step's exit code and ``::error::`` annotation.

Reads (env): ``SANDBOX`` and ``AGENT_ENV_FILE`` (exported by
``proxy/setup_sandbox.py`` via ``$GITHUB_ENV``), ``RUNNER_TEMP``,
``GITHUB_WORKSPACE``, ``GITHUB_OUTPUT``, ``TEND_MODEL``,
``TEND_EFFORT``, ``TEND_ARGS``, ``TEND_ALLOWED_TOOLS``,
``TEND_SYSTEM_PROMPT``, ``TEND_PROMPT``, ``TEND_TIMEOUT_SEC``,
``SHOW_FULL_OUTPUT``, ``BOT_NAME``, ``BOT_ID``, optional
``TEND_AUTO_MEMORY_SETTINGS``, ``CLAUDE_CODE_SUBPROCESS_ENV_SCRUB``, plus the
``GITHUB_*`` context from Actions. ``GITHUB_STEP_SUMMARY`` is read only when
rendering the transcript.
Publishes ``stream_json`` and, after supervision has stopped every sandbox
process, ``sandbox_reaped``. Used by the Claude harness action.

Decisions this module owns:

* **The supervisor times the run, so nothing infers the bound from an exit
  code.** Waiting with a timeout raises on the bound, which is the answer; a
  code cannot give it, since a killed agent and a crashing one both land on the
  same numbers and the agent may return them itself.
* **A zero exit does NOT mean the turn succeeded.** ``claude -p`` exits 0 on
  rate limits, max turns, auth failures and a failed final model request, and
  ``is_error: true`` occurs even on subtype ``success``. The last ``result``
  event decides; a turn with no result event never completed.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import _common
import _sandbox

#: Transcript lines rendered to the job summary, so a long session cannot flood it.
TRANSCRIPT_MAX_LINES = 400

#: Seconds the agent has to flush after the bound's TERM, before the KILL.
TERM_GRACE_SEC = 5

#: Stderr lines quoted on a failure, read from the last this many bytes.
STDERR_TAIL_LINES = 20
STDERR_TAIL_BYTES = 64 * 1024

#: Characters of the agent's own reason quoted in the failure annotation.
#: The reason is a whole assistant text block, which can be the agent's entire
#: final answer, and ``enrich_tend_outage_issues.py`` pastes these annotations
#: into one batched issue comment under a 64 KiB cap — so an unbounded reason
#: crowds out the other runs' rows.
REASON_MAX_CHARS = 500


def settings(allowed_tools: str) -> dict[str, Any]:
    """``.claude/settings.local.json`` for the run.

    ``permissions.allow`` is built from the comma-separated ``allowed_tools``
    input. The mode itself comes from ``--permission-mode`` on argv, not from
    here: Claude Code 2.1.257 ignores ``defaultMode: bypassPermissions`` in a
    project ``settings.json``/``settings.local.json`` and names the flag as the
    way to set it. The key is left in place as the declaration of intent that
    the flag carries out; the allow list is what a user- or managed-settings
    layer would still narrow against.

    ``skipDangerousModePermissionPrompt`` pre-accepts the one-time bypass-mode
    "I accept the risks" disclaimer — the key the dialog's accept button writes,
    read from any settings layer. No Stop/StopFailure hooks: headless detects
    completion from the process exit plus the result event. ``attribution``
    (which supersedes the deprecated ``includeCoAuthoredBy``) empties Claude
    Code's ``Co-Authored-By: Claude`` trailer and ``Generated with Claude Code``
    PR footer, so the bot's commits and PRs are attributed to the bot alone.
    """
    return {
        "permissions": {
            "defaultMode": "bypassPermissions",
            "allow": [tool.strip() for tool in allowed_tools.split(",")],
        },
        "skipDangerousModePermissionPrompt": True,
        "attribution": {"commit": "", "pr": ""},
    }


def launch_argv(
    *,
    sandbox: str,
    agent_env_file: str,
    model: str,
    effort: str = "",
    extra_args: str = "",
    allowed_tools: str,
    system_prompt: str,
    prompt: str,
    subprocess_env_scrub: str,
    bot_name: str,
    bot_id: str,
    ci: str,
    settings_file: str = "",
) -> list[str]:
    """The command that launches the agent as the non-sudo sandbox user.

    ``sudo env NAME=…`` replaces the environment with only what is listed, so
    :func:`_sandbox.launch_env` composes it; tend's own ``BOT_*``/``CI``
    assignments are the caller-appended names that docstring allows for.

    The model, tools and prompts are argv rather than environment: nothing on
    the far side reads them, and ``--permission-mode`` is what actually sets
    the mode — a project ``settings.local.json`` cannot, since Claude Code
    2.1.257 ignores ``defaultMode: bypassPermissions`` there.
    """
    agent_env = _sandbox.launch_env(agent_env_file)
    if settings_file:
        # The explicit experimental config field wins over an adopter's general
        # Claude setting. Leaving this variable in the launch env would make a
        # successful restore silently inert even though the injected settings
        # enable and redirect auto memory.
        agent_env = [
            entry
            for entry in agent_env
            if not entry.startswith("CLAUDE_CODE_DISABLE_AUTO_MEMORY=")
        ]
    argv = [
        "sudo",
        "-u",
        sandbox,
        "env",
        *agent_env,
        f"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB={subprocess_env_scrub}",
        f"BOT_NAME={bot_name}",
        f"BOT_ID={bot_id}",
        f"CI={ci}",
        "claude",
        "-p",
        *(arg for arg in extra_args.split("\n") if arg),
        "--model",
        model,
        "--permission-mode",
        "bypassPermissions",
        "--allowedTools",
        allowed_tools,
        "--append-system-prompt",
        system_prompt,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if effort:
        argv.extend(["--effort", effort])
    if settings_file:
        argv.extend(["--settings", settings_file])
    argv.append(prompt)
    return argv


@dataclass(frozen=True)
class Supervised:
    """How the supervised launch ended."""

    #: The agent's exit code, or None when the bound killed the run.
    exit_code: int | None
    elapsed: int


class Cancelled(BaseException):
    """The runner asked this process to stop, mid-supervision.

    A ``BaseException`` like ``KeyboardInterrupt``: it has to pass through an
    ``except Exception`` on its way to the reap rather than be caught as a
    failure of the run.
    """


@contextlib.contextmanager
def raise_on_cancel() -> Iterator[None]:
    """Turn SIGTERM and SIGINT into :class:`Cancelled` for the block's duration.

    A cancelled workflow — ``cancel-in-progress``, a maintainer pressing cancel
    — reaches this step as a signal. SIGTERM's default disposition ends the
    process where it stands, which would skip the reap below and leave the
    agent running as an orphan, still writing to the workspace, while the
    runner tears the job down. Raising instead routes the cancellation through
    the same ``finally`` every other exit takes.

    Restored on the way out, so a second signal during the reap ends the
    process outright, which is what an escalating runner means by it.
    """

    def cancel(number: int, frame: FrameType | None) -> None:
        raise Cancelled(f"signal {number}")

    previous = {
        number: signal.signal(number, cancel)
        for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def signal_sandbox(name: str, sandbox: str) -> None:
    """Send the signal *name* to every process the sandbox uid owns.

    By uid rather than to the child: the child is ``sudo``, which relays
    nothing, so a signal aimed at it reaches the agent not at all. Failure costs
    nothing — there may be no such process left, which is the outcome wanted.
    """
    subprocess.run(
        ["sudo", "pkill", f"-{name}", "-u", sandbox],
        stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def supervise(
    argv: list[str],
    *,
    sandbox: str,
    timeout_sec: int,
    stream_json: Path,
    stderr_log: Path,
) -> Supervised:
    """Run *argv* under the bound, capturing its streams to runner-owned files.

    The files are opened by this process on purpose: the sandbox writes through
    the inherited fds regardless of who owns them, so the run's record cannot be
    rewritten from the far side of the boundary.

    stdin is closed explicitly. A backgrounded command got ``/dev/null`` for
    free; this one is in the foreground and would inherit the step's, which
    costs ``claude -p`` a three-second wait for input that never comes and feeds
    it whatever does arrive.

    Overrunning the bound asks the agent to stop before making it: a TERM to the
    sandbox uid, :data:`TERM_GRACE_SEC` to flush, then the KILL. Without the
    grace the agent's session JSONL loses its tail mid-write, and that file is
    what the token accounting falls back on when a run produces no result event
    — the runs that time out are exactly the ones with no result event.

    The KILL is this function's ``finally`` and the only unconditional step: it
    is what actually stops a run the TERM did not, so no path out of here,
    exception included, may skip it. :func:`raise_on_cancel` is what makes
    "every path" include a cancelled job, which arrives as a signal rather than
    as anything Python would raise on its own.

    The KILL is followed by a wait on the child, so the step does not return
    while ``sudo`` and the agent under it are still alive: the steps after this
    one hand the workspace back and read the run's files, and a survivor would
    race them.
    """
    start = time.monotonic()
    agent: subprocess.Popen[bytes] | None = None
    try:
        with (
            raise_on_cancel(),
            stream_json.open("wb") as out,
            stderr_log.open("wb") as err,
        ):
            agent = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err
            )
            try:
                returncode = agent.wait(timeout_sec)
                # Shell convention: 128 + N when signal N killed the child.
                code = 128 - returncode if returncode < 0 else returncode
            except subprocess.TimeoutExpired:
                # The bound decides, however the stop then goes: an agent that
                # takes the TERM and one that has to be killed are the same run
                # to a maintainer, and the code it leaves says nothing.
                code = None
                signal_sandbox("TERM", sandbox)
                # `sudo` exits once its child does, so this observes the agent.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    agent.wait(TERM_GRACE_SEC)
    finally:
        signal_sandbox("KILL", sandbox)
        if agent is not None:
            agent.wait()
    return Supervised(code, round(time.monotonic() - start))


def stream_events(stream_json: Path) -> Iterator[dict[str, Any]]:
    """The run's events, streamed. A stream that was never written has none.

    Never a list: a session runs for hours with every tool result in this file,
    and each consumer below needs only one pass. Call it again for a second.
    """
    if not stream_json.exists():
        return iter(())
    return _common.read_ndjson(stream_json)


def _assistant_blocks(events: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """The content blocks of the assistant events, whatever else the stream holds.

    A truncated or synthetic event can carry a null ``message`` or a
    non-list ``content``; the verdict is the last thing that may die on one, so
    a shape this does not recognise contributes nothing rather than raising.
    """
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            yield from (block for block in content if isinstance(block, dict))


def failure_reason(events: Iterable[dict[str, Any]]) -> str:
    """The last non-blank assistant text, capped at :data:`REASON_MAX_CHARS`.

    A session-limit exit is non-zero and emits a ``<synthetic>`` assistant
    message ("You've hit your session limit · resets 8:30am (UTC)"), so the last
    assistant text names the cause; ``enrich_tend_outage_issues.py`` carries the
    annotation into the tend-outage issue. A ``tool_use`` block is not text and
    a tool name is not a failure cause, so only ``text`` blocks are considered,
    and a blank one is no reason at all.

    The cap is here rather than at the annotation because this is where the
    agent's own text enters: the block that names the cause is the same block
    that can be the agent's whole closing answer, and everything downstream
    quotes what this returns.
    """
    reason = ""
    for block in _assistant_blocks(events):
        if block.get("type") != "text":
            continue
        text = str(block.get("text", "")).strip()
        if text:
            reason = text
    if len(reason) > REASON_MAX_CHARS:
        return reason[:REASON_MAX_CHARS] + "…"
    return reason


def turn_outcome(events: Iterable[dict[str, Any]]) -> str | None:
    """Why the turn failed despite a zero exit, or None if it succeeded."""
    # A turn can emit more than one; the last is the turn's outcome.
    last: dict[str, Any] | None = None
    for event in events:
        if event.get("type") == "result":
            last = event
    if last is None:
        return "produced no result event — the turn did not complete"
    subtype = last.get("subtype")
    if last.get("is_error") is True or subtype != "success":
        named = "unknown" if subtype is None else subtype
        return (
            f"turn ended in failure ({named}) — "
            "rate limit, auth, max turns, or server error"
        )
    return None


def transcript(events: Iterable[dict[str, Any]], limit: int) -> list[str]:
    """A readable transcript — assistant text and tool calls — at most *limit* lines.

    The cap is applied while accumulating, not after: one event's text can carry
    thousands of lines, and a long session's whole stream must never be held
    just to throw most of it away.
    """
    lines: list[str] = []
    for block in _assistant_blocks(events):
        if block.get("type") == "text":
            lines.extend(str(block.get("text", "")).split("\n"))
        elif block.get("type") == "tool_use":
            # `ensure_ascii=False` because the jq this replaced emitted raw
            # UTF-8. Escaping spends six of the 200 characters on every
            # non-ASCII one, so a non-English tool input renders as `\u….`
            rendered = json.dumps(
                block.get("input"), separators=(",", ":"), ensure_ascii=False
            )[:200]
            lines.extend(f"→ {block.get('name')}: {rendered}".split("\n"))
        if len(lines) >= limit:
            return lines[:limit]
    return lines


def stderr_tail(stderr_log: Path) -> list[str]:
    """The agent's last words, quoted on every failure.

    Read from the end and split on newlines alone: the agent decides both how
    much it writes here and what is in it, and this runs on the path where the
    annotation matters most — so neither an unbounded log nor a vertical tab in
    it may cost the diagnostic.
    """
    if not stderr_log.exists():
        return []
    with stderr_log.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - STDERR_TAIL_BYTES))
        chunk = handle.read()
    lines = chunk.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines[-STDERR_TAIL_LINES:]


def _quote_stderr(stderr_log: Path) -> None:
    """Print the agent's last words where they cannot issue workflow commands.

    The annotation above them needs no such bracket: the reason is flattened to
    one line and embedded mid-line rather than starting one.
    """
    with _common.stop_commands():
        for line in stderr_tail(stderr_log):
            print(line, flush=True)


def verdict(
    *,
    claude_exit: int | None,
    stream_json: Path,
    stderr_log: Path,
    timeout_sec: str,
    show_full_output: str,
) -> int:
    """The step's exit code, with the annotation and job summary that explain it.

    ``claude_exit`` is None exactly when the bound killed the run.
    """
    if show_full_output == "true":
        lines = transcript(stream_events(stream_json), TRANSCRIPT_MAX_LINES)
        if lines:
            _common.append_summary(
                "## Claude transcript\n\n```\n" + "\n".join(lines) + "\n```"
            )

    if claude_exit is None:
        return _common.fail(f"Claude headless run exceeded {timeout_sec}s timeout")

    if claude_exit:
        reason = failure_reason(stream_events(stream_json))
        named = f": {reason}" if reason else ""
        _common.annotate(
            "error",
            f"claude -p exited non-zero (exit={claude_exit}){named}"
            " — see the session-logs artifact",
        )
        _quote_stderr(stderr_log)
        return claude_exit

    why = turn_outcome(stream_events(stream_json))
    if why is None:
        return 0
    _common.annotate("error", f"claude -p {why}")
    _quote_stderr(stderr_log)
    return 1


def main() -> int:
    env = _common.require_env(
        "SANDBOX",
        "AGENT_ENV_FILE",
        "RUNNER_TEMP",
        "GITHUB_WORKSPACE",
        "GITHUB_OUTPUT",
        "TEND_MODEL",
        "TEND_ALLOWED_TOOLS",
        "TEND_SYSTEM_PROMPT",
        "TEND_PROMPT",
        "TEND_TIMEOUT_SEC",
        "SHOW_FULL_OUTPUT",
        "BOT_NAME",
        "BOT_ID",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
    )
    sandbox = env["SANDBOX"]
    workspace = Path(env["GITHUB_WORKSPACE"])
    stream_json = Path(env["RUNNER_TEMP"]) / "tend-stream.json"
    stderr_log = Path(env["RUNNER_TEMP"]) / "tend-claude-stderr.log"

    # Written as the sandbox user so the agent can read it back. It lands in the
    # adopter's checkout untracked, next to the `.claude/skills/` they do track;
    # setup_sandbox.py's global gitignore for the sandbox user keeps a broad
    # `git add -A` from committing `bypassPermissions` into the session's PR.
    # `stdin` is closed on every `sudo` here: without a tty a `sudo` that needs
    # a password fails instead of waiting for one on the step's stdin. The `tee`
    # gets the same guarantee from `input=`, which binds its stdin to a pipe.
    subprocess.run(
        ["sudo", "-u", sandbox, "mkdir", "-p", str(workspace / ".claude")],
        stdin=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["sudo", "-u", sandbox, "tee", str(workspace / ".claude/settings.local.json")],
        input=json.dumps(settings(env["TEND_ALLOWED_TOOLS"])),
        stdout=subprocess.DEVNULL,
        text=True,
        check=True,
    )

    # The agent's launch env is $AGENT_ENV_FILE (written by setup_sandbox.py;
    # shared with the plugin-install step so the two can't drift): proxy
    # routing, CA trust for every client family, and DUMMY GitHub + Anthropic
    # credentials in the production schemes — the proxy replaces them with the
    # real secrets for their hosts. Non-allowlisted traffic tunnels through
    # untouched. No real secret is in this env.
    argv = launch_argv(
        sandbox=sandbox,
        agent_env_file=env["AGENT_ENV_FILE"],
        model=env["TEND_MODEL"],
        effort=os.environ.get("TEND_EFFORT", ""),
        extra_args=os.environ.get("TEND_ARGS", ""),
        allowed_tools=env["TEND_ALLOWED_TOOLS"],
        system_prompt=env["TEND_SYSTEM_PROMPT"],
        prompt=env["TEND_PROMPT"],
        subprocess_env_scrub=env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"],
        bot_name=env["BOT_NAME"],
        bot_id=env["BOT_ID"],
        ci=os.environ.get("CI") or "true",
        settings_file=os.environ.get("TEND_AUTO_MEMORY_SETTINGS", ""),
    )
    # Published before the launch: the path does not depend on the run, and the
    # steps that read it — Token usage, the session-logs artifact — are
    # `if: always()`, so they must still find it when the launch itself blows up.
    _common.set_output("stream_json", str(stream_json))

    try:
        run = supervise(
            argv,
            sandbox=sandbox,
            timeout_sec=int(env["TEND_TIMEOUT_SEC"]),
            stream_json=stream_json,
            stderr_log=stderr_log,
        )
    finally:
        # supervise() kills and reaps the sandbox uid on every exit, including
        # cancellation and launch failure. The memory save keys on this output
        # so it never reads agent-owned files while a sandbox process survives.
        _common.set_output("sandbox_reaped", "true")
    timed_out = run.exit_code is None
    print(
        f"Supervisor: status={'timeout' if timed_out else 'exited'} "
        f"elapsed={run.elapsed}s "
        f"claude_exit={'none' if timed_out else run.exit_code}",
        flush=True,
    )

    return verdict(
        claude_exit=run.exit_code,
        stream_json=stream_json,
        stderr_log=stderr_log,
        timeout_sec=env["TEND_TIMEOUT_SEC"],
        show_full_output=env["SHOW_FULL_OUTPUT"],
    )


if __name__ == "__main__":
    _common.run(main)
