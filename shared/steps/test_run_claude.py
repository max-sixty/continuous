"""The agent launch and the verdict it produces.

The launch tests replace ``subprocess.run``: what is under test is the crossing
this module composes — argv, env order, the runner-owned redirects, the bound
and the reap — not a second uid. ``proxy/test-setup-sandbox.sh``'s
``verify-launch`` phase drives the real thing on a hosted runner.

The verdict tests need no agent, no credential and no second uid — every input
is a file or a scalar — so each branch is reachable from a fixture.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import _sandbox
import pytest
import run_claude
from _fakes import GithubFiles


def _ev_text(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _ev_tool_use(name: str, **input_: Any) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": name, "input": input_}]
            },
        }
    )


def _ev_result(subtype: str = "success", *, is_error: bool = False) -> str:
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error})


# --- the verdict --------------------------------------------------------------

Verdict = Callable[..., tuple[int, str, str]]


@pytest.fixture
def verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], github_files: GithubFiles
) -> Verdict:
    """Reach a verdict; return (exit code, stdout, job summary).

    *stream* is the stream-json's literal content, or None to leave the file
    absent — a run that died before writing anything.
    """

    def go(
        *,
        stream: str | None = "",
        claude_exit: int | None = 0,
        show_full_output: str = "false",
        stderr_log: str = "boom: the agent's last words\n",
    ) -> tuple[int, str, str]:
        stream_json = tmp_path / "stream.json"
        if stream is not None:
            stream_json.write_text(stream)
        log = tmp_path / "stderr.log"
        log.write_text(stderr_log)
        code = run_claude.verdict(
            claude_exit=claude_exit,
            stream_json=stream_json,
            stderr_log=log,
            timeout_sec="900",
            show_full_output=show_full_output,
        )
        return code, capsys.readouterr().out, github_files.summary.read_text()

    return go


def test_verdict_reports_a_crash_that_left_no_assistant_text(verdict: Verdict) -> None:
    """A non-zero exit with nothing to quote must still annotate and exit truly.

    The annotation, the stderr tail and the agent's own exit code are the only
    diagnostics on this path.
    """
    code, out, _ = verdict(stream=_ev_result("error_during_execution"), claude_exit=7)

    assert code == 7, "the step must carry the agent's own exit code"
    assert "::error::claude -p exited non-zero (exit=7) — see the session-logs" in out
    assert "boom: the agent's last words" in out


def test_verdict_treats_whitespace_only_text_as_no_reason(verdict: Verdict) -> None:
    """Blank text must be filtered, not quoted into the annotation."""
    code, out, _ = verdict(stream=_ev_text("   "), claude_exit=2)

    assert code == 2
    assert "::error::claude -p exited non-zero (exit=2) — see the session-logs" in out


def test_verdict_survives_a_stream_json_that_was_never_written(
    verdict: Verdict,
) -> None:
    """A run that died before writing the stream still reports its exit code."""
    code, out, _ = verdict(stream=None, claude_exit=126)

    assert code == 126
    assert "::error::claude -p exited non-zero (exit=126)" in out


def test_verdict_names_the_cause_in_the_annotation(verdict: Verdict) -> None:
    """The last non-blank assistant text is what a maintainer triages from.

    enrich-tend-outage-issues.sh carries the annotation into the tend-outage
    issue, so "session limit" versus "auth failure" has to survive into it
    rather than being left in an artifact nobody downloads.
    """
    stream = "\n".join(
        [
            _ev_text("thinking about it"),
            _ev_text("You've hit your session limit · resets 8:30am (UTC)"),
            "",
        ]
    )

    code, out, _ = verdict(stream=stream, claude_exit=1)

    assert code == 1
    assert "session limit · resets 8:30am (UTC) — see the session-logs" in out
    assert "thinking about it" not in out, "only the LAST assistant text is quoted"


def test_verdict_keeps_a_multi_line_reason_on_one_line(verdict: Verdict) -> None:
    """An annotation ends at the first newline, and the runner parses the rest.

    A reason the agent wrote across several lines has to arrive flattened, or
    GitHub truncates it at the break and reads what follows as a fresh line of
    step output.
    """
    code, out, _ = verdict(
        stream=_ev_text("auth failed\n::error::forged"), claude_exit=1
    )

    assert code == 1
    assert (
        "::error::claude -p exited non-zero (exit=1): auth failed ::error::forged"
        " — see the session-logs artifact"
    ) in out
    assert "\n::error::forged" not in out


def test_verdict_bounds_the_reason_it_quotes(verdict: Verdict) -> None:
    """The reason is a whole assistant text block, so it can be the whole answer.

    `enrich-tend-outage-issues.sh` pastes these annotations into one batched
    issue comment under a 64 KiB cap; unbounded, one run's closing summary
    crowds out the rows for every other run in the batch.
    """
    code, out, _ = verdict(stream=_ev_text("x" * 2000), claude_exit=1)

    assert code == 1
    quoted = out.split("(exit=1): ", 1)[1].split(" — see the session-logs", 1)[0]
    assert quoted == "x" * run_claude.REASON_MAX_CHARS + "…"


def test_verdict_disarms_workflow_commands_in_the_agents_stderr(
    verdict: Verdict,
) -> None:
    """The agent writes its own stderr, and the runner parses `::` lines.

    Without the stop-commands bracket a prompt-injected agent posts its own
    annotations, masks whatever it likes out of the log, or leaves command
    processing off for every step after this one.
    """
    code, out, _ = verdict(
        claude_exit=3,
        stderr_log="::error::posted by the agent\n::add-mask::secret\n",
    )

    assert code == 3
    before, injected = out.split("::error::posted by the agent", 1)
    token = before.rsplit("::stop-commands::", 1)[1].strip()
    assert token, "the agent's stderr was echoed with commands still live"
    assert injected.strip().endswith(f"::{token}::"), (
        "command processing was left switched off for the steps that follow"
    )


def test_verdict_quotes_the_last_twenty_newline_delimited_stderr_lines(
    verdict: Verdict,
) -> None:
    """The agent decides what is in its stderr, and `\v` is not a line break.

    Counting Python's idea of a line — which breaks on `\v`, `\f`, `\x85` and
    U+2028 — hands the agent a way to push real output out of the window the
    maintainer reads.
    """
    log = "\n".join(["first"] + [f"line {i}\vcontinued" for i in range(19)]) + "\n"

    _, out, _ = verdict(claude_exit=1, stderr_log=log)

    assert "first" in out, "a vertical tab pushed a real line out of the tail"
    assert "line 0\vcontinued" in out


def test_verdict_passes_a_clean_turn(verdict: Verdict) -> None:
    code, out, _ = verdict(stream=_ev_result() + "\n")

    assert code == 0
    assert "::error::" not in out


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (_ev_text("got partway"), "produced no result event"),
        (_ev_result("error_max_turns"), "ended in failure (error_max_turns)"),
        (_ev_result("success", is_error=True), "ended in failure (success)"),
    ],
    ids=["no-result", "error-subtype", "is-error-on-success"],
)
def test_verdict_fails_a_zero_exit_that_did_not_finish(
    verdict: Verdict, stream: str, expected: str
) -> None:
    """`claude -p` exits 0 on rate limits, max turns, and auth failures.

    Trusting the exit code alone would mark the notification read and let the
    outage report skip, so a turn that never reached a good `result` event is
    a failure regardless of what the process returned.
    """
    code, out, _ = verdict(stream=stream)

    assert code == 1, f"a turn that {expected} must fail the step"
    assert expected in out


def test_verdict_names_one_cause_when_the_stream_is_unreadable(
    verdict: Verdict,
) -> None:
    """An absent stream on the zero-exit path is a failure, named once."""
    code, out, _ = verdict(stream=None)

    assert code == 1
    assert "::error::claude -p produced no result event" in out
    assert out.count("::error::") == 1, f"expected exactly one annotation: {out!r}"
    assert "ended in failure" not in out, "reported as the wrong cause"


def test_verdict_survives_an_event_shape_it_does_not_recognise(
    verdict: Verdict,
) -> None:
    """The verdict is the last thing that may die on a malformed event.

    A stream torn mid-write, or a synthetic event with a null `message`, must
    cost nothing: raising here loses the annotation and the agent's own exit
    code, which are the only diagnostics the failure path has.
    """
    stream = "\n".join(
        [
            json.dumps({"type": "assistant", "message": None}),
            json.dumps({"type": "assistant", "message": "a string, not an object"}),
            json.dumps({"type": "assistant", "message": {"content": "not a list"}}),
            json.dumps({"type": "assistant"}),
            _ev_text("the real reason"),
            "",
        ]
    )

    code, out, _ = verdict(stream=stream, claude_exit=5)

    assert code == 5
    assert "(exit=5): the real reason — see the session-logs" in out


def test_verdict_reads_the_last_result_event(verdict: Verdict) -> None:
    """A turn can emit more than one; the last one is the turn's outcome."""
    stream = "\n".join([_ev_result("error_max_turns"), _ev_result(), ""])

    code, out, _ = verdict(stream=stream)

    assert code == 0, f"an earlier failed result event decided the turn: {out!r}"


def test_verdict_never_quotes_a_tool_call_as_the_reason(verdict: Verdict) -> None:
    """A tool_use block is not text, and a tool name is not a failure cause."""
    stream = "\n".join([_ev_text("real reason"), _ev_tool_use("Bash"), ""])

    code, out, _ = verdict(stream=stream, claude_exit=1)

    assert code == 1
    assert "real reason — see the session-logs" in out
    assert "Bash" not in out


def test_verdict_reports_the_supervisor_timeout(verdict: Verdict) -> None:
    """The bound is the absence of an exit code, never a particular one.

    Reading a code would put "exited non-zero" in the outage issue for a run
    that simply needs a bigger `timeout_seconds`, and the agent can return any
    code the supervisor might have claimed for itself.
    """
    code, out, _ = verdict(
        claude_exit=None, stream=_ev_text("some text the agent got out first")
    )

    assert code == 1
    assert "exceeded 900s timeout" in out, (
        "the annotation names the bound so the fix (raise timeout_seconds) is "
        "readable without opening the workflow"
    )
    assert "exited non-zero" not in out, "a bound overrun reported as a crash"


def test_verdict_does_not_read_the_agents_own_124_as_a_timeout(
    verdict: Verdict,
) -> None:
    """124 was `timeout(1)`'s convention, not a code the agent cannot return.

    Inferring the bound from the exit code would swallow the agent's own
    account of why it stopped, which is what the outage issue is built from.
    """
    code, out, _ = verdict(claude_exit=124, stream=_ev_text("I hit an internal error"))

    assert code == 124
    assert "I hit an internal error" in out
    assert "timeout" not in out


def test_verdict_renders_the_transcript_only_when_asked(verdict: Verdict) -> None:
    stream = "\n".join(
        [_ev_text("hello there"), _ev_tool_use("Bash", command="ls"), _ev_result(), ""]
    )

    _, _, off = verdict(stream=stream, show_full_output="false")
    _, _, on = verdict(stream=stream, show_full_output="true")

    assert off == "", "show_full_output=false must leave the job summary alone"
    assert "## Claude transcript" in on
    assert "hello there" in on
    assert '→ Bash: {"command":"ls"}' in on


def test_the_rendered_transcript_is_capped(verdict: Verdict) -> None:
    """A long session must not flood the job summary.

    The cap counts rendered lines, not events: one event's text can carry
    thousands, so an event-level cap would let a single turn through.
    """
    stream = "\n".join(
        [_ev_text("\n".join(f"line {i}" for i in range(1000))), _ev_result(), ""]
    )

    _, _, summary = verdict(stream=stream, show_full_output="true")

    body = summary.split("```")[1].strip().splitlines()
    assert len(body) == run_claude.TRANSCRIPT_MAX_LINES
    assert body[0] == "line 0" and body[-1] == "line 399"


# --- the launch ---------------------------------------------------------------


@dataclass
class Recorded:
    argv: list[str]
    kwargs: dict[str, Any]


@dataclass
class FakeAgent:
    """A launched `sudo` whose `wait` follows the scenario, recording the order.

    Every wait goes into the shared call log beside the signals, so a test reads
    one ordered sequence rather than correlating two lists.
    """

    calls: list[Recorded]
    returncode: int
    times_out: bool
    term_ends_it: bool

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(Recorded(["wait"], {"timeout": timeout}))
        if timeout is None:
            # The reap after the KILL. SIGKILL cannot be blocked, so the agent
            # is already gone and `sudo` exits with it — this returns at once
            # whatever the run did before.
            return -9 if self.times_out else self.returncode
        if not self.times_out:
            return self.returncode
        if len([c for c in self.calls if c.argv == ["wait"]]) == 1:
            raise subprocess.TimeoutExpired("claude", timeout)
        if self.term_ends_it:
            return -15
        raise subprocess.TimeoutExpired("claude", timeout)


@dataclass
class Launch:
    calls: list[Recorded]
    code: int
    out: str
    summary: str
    stream_json: Path
    stderr_log: Path

    def command(self, program: str) -> Recorded:
        hits = [call for call in self.calls if program in call.argv]
        assert len(hits) == 1, f"expected one {program} call, got {hits}"
        return hits[0]

    def supervision(self) -> list[str]:
        """The waits and the signals between them, in the order they happened."""
        steps = []
        for call in self.calls:
            if call.argv == ["wait"]:
                steps.append(f"wait({call.kwargs['timeout']})")
            elif "pkill" in call.argv:
                steps.append(" ".join(call.argv[1:3]))
        return steps


Launcher = Callable[..., Launch]


@pytest.fixture
def launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    github_files: GithubFiles,
) -> Launcher:
    """Drive `main()` with every subprocess replaced; report what it did."""

    def go(
        *,
        stream: str = "",
        stderr_text: str = "",
        returncode: int = 0,
        timed_out: bool = False,
        term_ends_it: bool = True,
        launch_error: Exception | None = None,
        agent_env_text: str = "HOME=/sandbox\nGITHUB_TOKEN=dummy\nPATH=/usr/bin\n",
        **overrides: str,
    ) -> Launch:
        runner_temp = tmp_path / "runner-temp"
        workspace = tmp_path / "workspace"
        agent_env = tmp_path / "agent-env"
        runner_temp.mkdir(exist_ok=True)
        workspace.mkdir(exist_ok=True)
        agent_env.write_text(agent_env_text)

        # github_files owns the withheld names; clearing the rest makes the
        # composed argv the fixture's, not the developer's shell's.
        for name in list(os.environ):
            if name.startswith("GITHUB_") and name not in _sandbox.WITHHELD:
                monkeypatch.delenv(name)
        monkeypatch.setenv("GITHUB_TOKEN", "the-real-pat")
        monkeypatch.setenv("GITHUB_WORKFLOW", "tend-weekly")
        monkeypatch.setenv("CI", "true")
        env = {
            "SANDBOX": "tend-sandbox",
            "AGENT_ENV_FILE": str(agent_env),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_WORKSPACE": str(workspace),
            "TEND_MODEL": "opus",
            "TEND_ALLOWED_TOOLS": "Bash, Read",
            "TEND_SYSTEM_PROMPT": "tend directives",
            "TEND_PROMPT": "review the PR",
            "TEND_TIMEOUT_SEC": "900",
            "SHOW_FULL_OUTPUT": "false",
            "BOT_NAME": "tend-bot",
            "BOT_ID": "42",
            "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "0",
            **overrides,
        }
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        calls: list[Recorded] = []

        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            calls.append(Recorded(list(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0)

        def fake_popen(argv: list[str], **kwargs: Any) -> FakeAgent:
            calls.append(Recorded(list(argv), kwargs))
            if launch_error is not None:
                raise launch_error
            kwargs["stdout"].write(stream.encode())
            kwargs["stderr"].write(stderr_text.encode())
            return FakeAgent(calls, returncode, timed_out, term_ends_it)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        try:
            code = run_claude.main()
        except BaseException:
            assert any("pkill" in call.argv for call in calls), (
                "the launch failed and the sandbox uid was never reaped"
            )
            raise
        return Launch(
            calls,
            code,
            capsys.readouterr().out,
            github_files.summary.read_text(),
            runner_temp / "tend-stream.json",
            runner_temp / "tend-claude-stderr.log",
        )

    return go


def test_launch_steers_the_agent_entirely_through_argv(launch: Launcher) -> None:
    """Nothing on the far side reads the model or the prompts from the env.

    `--permission-mode` restates what settings.local.json already says, so the
    mode survives an adopter overriding that file.
    """
    argv = launch(stream=_ev_result()).command("claude").argv

    assert argv[argv.index("claude") :] == [
        "claude",
        "-p",
        "--model",
        "opus",
        "--permission-mode",
        "bypassPermissions",
        "--allowedTools",
        "Bash, Read",
        "--append-system-prompt",
        "tend directives",
        "--output-format",
        "stream-json",
        "--verbose",
        "review the PR",
    ]


def test_launch_adds_the_restored_auto_memory_settings(launch: Launcher) -> None:
    settings_file = "/home/tend-sandbox/run/auto-memory/.tend-settings.json"
    result = launch(
        stream=_ev_result(),
        TEND_AUTO_MEMORY_SETTINGS=settings_file,
        agent_env_text=(
            "HOME=/sandbox\n"
            "GITHUB_TOKEN=dummy\n"
            "PATH=/usr/bin\n"
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1\n"
        ),
    )
    argv = result.command("claude").argv

    assert argv[-3:] == ["--settings", settings_file, "review the PR"]
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" not in argv


def test_launch_composes_the_file_then_the_context_then_tends_own_names(
    launch: Launcher,
) -> None:
    """`sudo env` replaces the environment, so this list is all the agent gets.

    The order is `_sandbox.launch_env`'s postcondition, plus tend's own
    assignments last; this pins the whole crossing as one argv.
    """
    result = launch(stream=_ev_result())
    argv = result.command("claude").argv
    crossing = argv[argv.index("env") + 1 : argv.index("claude")]

    assert crossing == [
        "HOME=/sandbox",
        "GITHUB_TOKEN=dummy",
        "PATH=/usr/bin",
        "GITHUB_WORKFLOW=tend-weekly",
        f"GITHUB_WORKSPACE={result.stream_json.parent.parent / 'workspace'}",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0",
        "BOT_NAME=tend-bot",
        "BOT_ID=42",
        "CI=true",
    ]
    assert argv[:4] == ["sudo", "-u", "tend-sandbox", "env"]


def test_launch_writes_the_settings_as_the_sandbox_user(launch: Launcher) -> None:
    """The agent has to read it back, and its uid is not the runner's."""
    result = launch(stream=_ev_result())
    tee = result.command("tee")

    assert tee.argv[:3] == ["sudo", "-u", "tend-sandbox"]
    assert tee.argv[-1].endswith("/.claude/settings.local.json")
    assert json.loads(tee.kwargs["input"]) == {
        "permissions": {
            "defaultMode": "bypassPermissions",
            "allow": ["Bash", "Read"],
        },
        "skipDangerousModePermissionPrompt": True,
        "attribution": {"commit": "", "pr": ""},
    }
    mkdir = result.command("mkdir")
    assert mkdir.argv[:3] == ["sudo", "-u", "tend-sandbox"]
    assert mkdir.kwargs["stdin"] is subprocess.DEVNULL, (
        "without a tty, a `sudo` that wants a password waits on the step's stdin"
    )


def test_launch_captures_the_streams_into_runner_owned_files(
    launch: Launcher,
) -> None:
    """The redirects are this process's: the sandbox writes through inherited fds.

    stdin is closed explicitly — inheriting the step's costs `claude -p` a
    three-second wait for input that never comes and feeds it whatever arrives.
    """
    result = launch(stream=_ev_result(), stderr_text="a warning\n")
    kwargs = result.command("claude").kwargs

    assert kwargs["stdin"] is subprocess.DEVNULL
    assert result.supervision() == ["wait(900)", "pkill -KILL", "wait(None)"]
    assert result.stream_json.read_text() == _ev_result()
    assert result.stderr_log.read_text() == "a warning\n"
    assert re.fullmatch(
        r"Supervisor: status=exited elapsed=\d+s claude_exit=0",
        result.out.splitlines()[0],
    ), result.out


def test_launch_publishes_the_stream_json_output(
    launch: Launcher, github_files: GithubFiles
) -> None:
    """The Token usage step and the session-logs artifact both read it."""
    result = launch(stream=_ev_result())

    assert github_files.outputs() == {
        "stream_json": str(result.stream_json),
        "sandbox_reaped": "true",
    }


def test_launch_reaps_the_sandbox_uid_after_every_run(launch: Launcher) -> None:
    """`sudo` relays nothing, so only a signal by uid reaches the agent."""
    for result in (
        launch(stream=_ev_result()),
        launch(stream=_ev_result("error_max_turns")),
        launch(timed_out=True),
    ):
        assert result.supervision()[-2:] == ["pkill -KILL", "wait(None)"], (
            "the step returned while `sudo` and the agent under it were still "
            "alive, racing the steps that hand the workspace back"
        )
        assert [c for c in result.calls if "pkill" in c.argv][-1].argv == [
            "sudo",
            "pkill",
            "-KILL",
            "-u",
            "tend-sandbox",
        ]


def test_launch_asks_the_agent_to_stop_before_making_it(launch: Launcher) -> None:
    """The bound gives the agent a TERM and a grace period, then kills it.

    Without the grace the agent's session JSONL loses its tail mid-write — and
    that file is what the token accounting falls back on for a run that produced
    no result event, which is every run that times out.
    """
    result = launch(timed_out=True, term_ends_it=True)

    assert result.supervision() == [
        "wait(900)",
        "pkill -TERM",
        "wait(5)",
        "pkill -KILL",
        "wait(None)",
    ]
    assert result.code == 1
    assert "Supervisor: status=timeout" in result.out


def test_launch_kills_a_run_the_term_does_not_end(launch: Launcher) -> None:
    """An agent deep in a tool call ignores the TERM; the KILL is the backstop.

    Reported the same either way: how the stop went says nothing a maintainer
    would act on, and the code such a run leaves says less.
    """
    result = launch(timed_out=True, term_ends_it=False)

    assert result.supervision() == [
        "wait(900)",
        "pkill -TERM",
        "wait(5)",
        "pkill -KILL",
        "wait(None)",
    ]
    assert result.code == 1
    assert "::error::Claude headless run exceeded 900s timeout" in result.out
    assert "claude_exit=none" in result.out


def test_launch_reaps_the_sandbox_uid_even_when_the_launch_itself_fails(
    launch: Launcher,
) -> None:
    """The reap is what stops the agent, so no path out of the supervisor skips it.

    A launch that cannot start — no `sudo`, no permission — is the case where a
    reap placed after the call would be skipped, leaving sandbox-uid processes
    running into the steps that follow.
    """
    with pytest.raises(FileNotFoundError):
        launch(launch_error=FileNotFoundError("sudo"))


def test_supervise_reaps_the_sandbox_uid_when_the_runner_cancels_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled job arrives as a signal, which no `finally` sees by itself.

    SIGTERM's default disposition ends this process where it stands, so the
    reap would be skipped and the agent would run on as an orphan — still
    writing to the workspace — while the runner tore the job down. `tend-triage`
    runs with `cancel-in-progress: true`, so this is a routine exit, not an
    exotic one.
    """
    calls: list[Recorded] = []
    handlers: list[Any] = []

    class CancelledAgent:
        """Raises on the supervised wait, the way the installed handler does."""

        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            handlers.append(signal.getsignal(signal.SIGTERM))
            if self.waits == 1:
                raise run_claude.Cancelled("signal 15")
            return -9

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(Recorded(list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0)

    agent = CancelledAgent()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: agent)
    before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(run_claude.Cancelled):
        run_claude.supervise(
            ["sudo", "-u", "tend-sandbox", "claude"],
            sandbox="tend-sandbox",
            timeout_sec=900,
            stream_json=tmp_path / "stream.json",
            stderr_log=tmp_path / "stderr.log",
        )

    assert handlers[0] is not before, "the supervised wait ran on the default handler"
    assert signal.getsignal(signal.SIGTERM) is before, (
        "the handler outlived the supervision it was installed for"
    )
    assert [call.argv for call in calls] == [
        ["sudo", "pkill", "-KILL", "-u", "tend-sandbox"]
    ]
    assert agent.waits == 2, "`sudo` was left unreaped after the KILL"


def test_launch_reports_a_signalled_child_in_shell_convention(
    launch: Launcher,
) -> None:
    """A negative returncode is Python's convention, not an exit status.

    Handing it back unchanged would exit the step 247 for a SIGKILL and put
    "exit=-9" in the outage issue.
    """
    result = launch(returncode=-9, stream=_ev_text("killed mid-turn"))

    assert result.code == 137
    assert "claude -p exited non-zero (exit=137): killed mid-turn" in result.out


def test_main_refuses_to_start_without_an_input_it_needs_late(
    monkeypatch: pytest.MonkeyPatch, github_files: GithubFiles
) -> None:
    """A missing input must fail now, not on the run that first reaches it.

    TEND_TIMEOUT_SEC is read only when naming the bound in a timeout
    annotation, so dropping it from the step's `env:` would leave every
    ordinary run green and surface months later, mid-outage, as a bare exit 1.
    """
    for name in (
        "SANDBOX",
        "AGENT_ENV_FILE",
        "RUNNER_TEMP",
        "GITHUB_WORKSPACE",
        "TEND_MODEL",
        "TEND_ALLOWED_TOOLS",
        "TEND_SYSTEM_PROMPT",
        "TEND_PROMPT",
        "SHOW_FULL_OUTPUT",
        "BOT_NAME",
        "BOT_ID",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
    ):
        monkeypatch.setenv(name, "set")
    monkeypatch.delenv("TEND_TIMEOUT_SEC", raising=False)

    with pytest.raises(SystemExit, match="TEND_TIMEOUT_SEC"):
        run_claude.main()
