"""Tests for the OAuth wrapper's extractors.

Run: ``uv run pytest`` from the repo root, which covers every Python suite.

`complete_match` and `first_url` are pure functions over whatever bytes the pty
happens to deliver, and every way they can be wrong ends the same way: a
credential that is stored and authenticates nothing, or a run that discards a
token after an approval has already been spent. Nothing downstream re-checks
them, so they are pinned here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from oauth_token import (
    ANSI_CSI,
    AUTHORIZE_URL,
    MAX_TOKEN_LENGTH,
    PASTE_PROMPT,
    TIMEOUT_SECONDS,
    TOKEN,
    TUI_ERROR,
    complete_match,
    failure_message,
    first_url,
    stop_and_reap,
)

URL = (
    b"https://claude.com/cai/oauth/authorize?code=true&client_id=9d1"
    b"&code_challenge=AAA&code_challenge_method=S256&state=BBB"
)
TOKEN_BYTES = b"sk-ant-oat01-" + b"A" * 95


def visible(buf: bytes) -> bytes:
    return ANSI_CSI.sub(b"", buf)


def test_osc8_hyperlink_yields_one_url() -> None:
    # The TUI emits the URL as an OSC 8 hyperlink: target and visible text are
    # the same string back to back with no separator, so a match runs straight
    # out of one into the next and hands the user a doubled, invalid URL.
    buf = b"\x1b]8;id=x;" + URL + URL + b"\x1b]8;;\r\n"
    assert first_url(visible(buf)) == URL


def test_partial_url_is_withheld() -> None:
    # Announcing a URL cut off by a read boundary sends the user to an
    # authorize page missing its PKCE challenge.
    assert first_url(visible(b"\x1b]8;id=x;" + URL[:60])) is None


def test_url_without_state_is_withheld() -> None:
    truncated = URL[: URL.index(b"&state=")] + b" "
    assert first_url(visible(truncated)) is None


def test_token_at_buffer_end_is_withheld() -> None:
    # The next read may extend it; accepting here stores a prefix.
    assert complete_match(TOKEN, TOKEN_BYTES) is None


def test_token_with_trailing_output_is_complete() -> None:
    assert complete_match(TOKEN, TOKEN_BYTES + b"\r\nStore this token") == TOKEN_BYTES


def test_token_split_by_a_read_boundary_is_withheld() -> None:
    # The failure the guard exists for: a fragment long enough to satisfy the
    # minimum length still looks like a whole token.
    assert complete_match(TOKEN, TOKEN_BYTES[:95]) is None


def test_token_styled_mid_run_is_recovered() -> None:
    # Ink restyles mid-frame. On the raw buffer an SGR sequence inside the
    # token's byte run makes the match miss entirely, and the run reports "no
    # token" after the approval has been spent.
    styled = TOKEN_BYTES[:60] + b"\x1b[0m" + TOKEN_BYTES[60:] + b"\r\n"
    assert complete_match(TOKEN, styled) is None
    assert complete_match(TOKEN, visible(styled)) == TOKEN_BYTES


def test_overlong_token_is_not_truncated_to_the_ceiling() -> None:
    # A bounded pattern would match the first MAX_TOKEN_LENGTH bytes, and the
    # following byte would make `complete_match` call that truncation final —
    # shipping a silently shortened credential. The pattern stays open-ended so
    # the length check in main() sees the real length and fails loudly.
    overlong = b"sk-ant-oat01-" + b"A" * (MAX_TOKEN_LENGTH + 50)
    assert complete_match(TOKEN, overlong + b"\r\n") == overlong
    assert len(overlong) > MAX_TOKEN_LENGTH


def test_authorize_url_pattern_stops_at_control_bytes() -> None:
    assert AUTHORIZE_URL.search(URL + b"\x1b]8;;").group(0) == URL


def test_unapproved_run_is_not_blamed_on_the_flag_it_was_given() -> None:
    # The skill's own invocation always passes --code-file, so a message telling
    # the caller to pass it names the one thing that was not wrong, and the
    # caller reruns unchanged into the same empty window.
    message = failure_message("/tmp/tend-oauth-code", None, True)
    assert "went unapproved" in message
    assert "rerun with --code-file" not in message


def test_a_refused_code_reads_differently_from_no_code_at_all() -> None:
    message = failure_message("/tmp/tend-oauth-code", 1.0, True)
    assert "no token came back" in message
    assert "went unapproved" not in message


def test_the_tuis_own_error_outranks_every_inference() -> None:
    # The wrapper used to guess at why a code failed; the CLI says so outright,
    # and the guess it replaced ("the authorize page rejected it") sent the
    # caller to re-approve a URL when the real cause was a code already spent.
    message = failure_message("/tmp/tend-oauth-code", 1.0, True, "status code 400")
    assert "status code 400" in message


def test_tui_error_matches_the_line_as_ink_renders_it() -> None:
    # Ink lays adjacent text nodes out with cursor moves, so stripping CSI
    # closes the gaps: a pattern written against the pretty version misses.
    assert TUI_ERROR.search(b"OAuth error: Request failed with status code 400")
    assert TUI_ERROR.search(b"OAutherror:Request failed with status code 400")


def test_tui_error_captures_only_its_own_line() -> None:
    match = TUI_ERROR.search(b"OAuth error: status code 400\r\nPress Enter to retry.")
    assert match.group(1).strip() == b"status code 400"


def test_paste_prompt_matches_the_stripped_render() -> None:
    assert PASTE_PROMPT.search(b"Pastecodehereifprompted>")
    assert PASTE_PROMPT.search(b"Paste code here if prompted >")


def test_a_run_with_nowhere_to_receive_a_code_is_told_to_pass_one() -> None:
    assert "rerun with --code-file" in failure_message(None, None, True)


def test_a_child_that_offered_no_url_is_not_reported_as_unapproved() -> None:
    # The read loop also breaks on a closed pty and on an exited child, so a
    # `claude` that dies early — no `setup-token` subcommand, a crash — reaches
    # here in a moment. Blaming the browser there sends the caller to approve a
    # URL that was never printed, and the child's own error is swallowed.
    message = failure_message("/tmp/tend-oauth-code", None, False)
    assert "run that command directly" in message
    assert "unapproved" not in message
    assert str(TIMEOUT_SECONDS) not in message


def test_stop_and_reap_finishes_a_child_that_ignores_termination() -> None:
    child_code = """
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(2)
"""
    with subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline() == "ready\n"
        stop_and_reap(child, terminate_timeout=0.01)
        assert child.returncode is not None


def test_stop_and_reap_finishes_the_process_group(tmp_path: Path) -> None:
    grandchild_pid_file = tmp_path / "grandchild.pid"
    grandchild_code = f"""
import os
import signal
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid()))
time.sleep(5)
"""
    child_code = f"""
import subprocess
import sys

subprocess.Popen([sys.executable, "-c", {grandchild_code!r}])
"""
    with subprocess.Popen(
        [sys.executable, "-c", child_code], start_new_session=True
    ) as child:
        try:
            deadline = time.monotonic() + 2
            while not grandchild_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert grandchild_pid_file.exists(), "grandchild did not start"
            grandchild_pid = int(grandchild_pid_file.read_text())
            child.wait(timeout=2)

            stop_and_reap(child, terminate_timeout=0.01)

            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("grandchild outlived its process-group owner")
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_interrupting_the_wrapper_does_not_orphan_its_child(
    tmp_path: Path, interrupt: signal.Signals
) -> None:
    child_pid_file = tmp_path / "child.pid"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        f"""#!{sys.executable}
import os
import signal
import time
from pathlib import Path

signal.signal(signal.SIGHUP, signal.SIG_IGN)
Path(os.environ["TEND_TEST_CHILD_PID_FILE"]).write_text(str(os.getpid()))
time.sleep(5)
"""
    )
    fake_claude.chmod(0o755)
    wrapper = subprocess.Popen(
        [sys.executable, Path(__file__).with_name("oauth_token.py")],
        env=os.environ
        | {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TEND_TEST_CHILD_PID_FILE": str(child_pid_file),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 2
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_file.exists(), "fake claude did not start"
        child_pid = int(child_pid_file.read_text())

        wrapper.send_signal(interrupt)
        wrapper.wait(timeout=2)

        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("oauth child outlived its interrupted wrapper")
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


TUI_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1"
    "&code_challenge=AAA&code_challenge_method=S256&state=BBB"
)
FAKE_TOKEN = "sk-ant-oat01-" + "A" * 95


def run_wrapper(
    tmp_path: Path, script: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """The wrapper against a fake `claude` that renders a scripted TUI."""
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(f"#!{sys.executable}\n{script}")
    fake_claude.chmod(0o755)
    return subprocess.run(
        [sys.executable, str(Path(__file__).with_name("oauth_token.py")), *args],
        env=os.environ | {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_rejected_code_fails_now_rather_than_at_the_deadline(
    tmp_path: Path,
) -> None:
    # The failure this guard exists for. `claude setup-token` reports a rejected
    # code and then waits at "Press Enter to retry", emitting nothing further —
    # so a wrapper watching only for a token sits out the rest of its window,
    # and the caller cannot tell that from an approval still pending. It has to
    # end at the error, carrying the CLI's own words.
    # The fake writes the code itself, after the prompt is up: the wrapper
    # unlinks any code file it finds at startup, since a code that predates the
    # run belongs to an earlier challenge and is already dead.
    code_file = tmp_path / "code"
    result = run_wrapper(
        tmp_path,
        f"""
import pathlib, sys, time
print({TUI_URL!r}, flush=True)
print("Paste code here if prompted >", flush=True)
pathlib.Path({str(code_file)!r}).write_text("SPENTcode#BBB")
sys.stdin.readline()
print("OAuth error: Request failed with status code 400", flush=True)
print("Press Enter to retry.", flush=True)
time.sleep(60)
""",
        "--code-file",
        str(code_file),
    )
    assert result.returncode != 0
    assert "status code 400" in result.stderr
    assert result.stdout == ""


def test_a_code_is_typed_even_when_the_prompt_never_renders(tmp_path: Path) -> None:
    # The paste path must not depend on matching the TUI's prompt wording, which
    # is free to change under us: the fallback would go quiet exactly when the
    # localhost callback has already failed.
    code_file = tmp_path / "code"
    result = run_wrapper(
        tmp_path,
        f"""
import pathlib, sys
print({TUI_URL!r}, flush=True)
pathlib.Path({str(code_file)!r}).write_text("GOODcode#BBB")
line = sys.stdin.readline()
assert "GOODcode#BBB" in line, line
print("Your OAuth token (valid for 1 year):" + {FAKE_TOKEN!r}, flush=True)
print("Store this token securely.", flush=True)
""",
        "--code-file",
        str(code_file),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == FAKE_TOKEN


def test_the_localhost_callback_path_needs_no_code_at_all(tmp_path: Path) -> None:
    # The common run: the CLI takes the redirect on its own listener and prints
    # the token with nothing typed. Nothing may block that on a paste.
    result = run_wrapper(
        tmp_path,
        f"""
print({TUI_URL!r}, flush=True)
print("Paste code here if prompted >", flush=True)
print("Your OAuth token (valid for 1 year):" + {FAKE_TOKEN!r}, flush=True)
print("Store this token securely.", flush=True)
""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == FAKE_TOKEN
