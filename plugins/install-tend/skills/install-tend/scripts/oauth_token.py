#!/usr/bin/env python3
"""Obtain a long-lived Claude Code OAuth token via `claude setup-token`.

Prints the token to stdout and everything else to stderr, so stdout pipes
straight into a consumer without the rest of the run coming with it.

`claude setup-token` renders its TUI with Ink, which draws only when stdout
is a TTY, so the child needs a pty. It then completes one of two ways. Normally
the CLI's own localhost listener takes the redirect and the run finishes with
no input at all — approving in the browser is the whole of the user's job.
Failing that, the browser lands on a page showing a `code#state` string and the
TUI waits at its paste prompt. Both paths have to stay open, which is why this
drives the pty itself rather than through script(1) — script(1) reads terminal
attributes from its own stdin and aborts with "tcgetattr: Operation not
supported on socket" when that is anything but a tty or /dev/null, and a
/dev/null stdin leaves the paste prompt unanswerable.

The authorize URL goes to stderr as soon as the TUI offers it, so a caller
running this in the background can hand it to the user. So does the TUI's own
error line, which is the difference between a run that is waiting for a person
and one that has already failed — indistinguishable from outside otherwise,
since a rejected code leaves the TUI sitting at "Press Enter to retry" for the
rest of the window. Nothing else from the TUI is echoed: its output carries the
token.

Usage:
    oauth_token.py [--code-file PATH]

`--code-file` names a path to watch. Write the `code#state` string there and it
is typed into the TUI, for the runs where the browser shows a code instead of
returning to the CLI. Most runs never need it.
"""

import argparse
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time

# Codes from the authorize page expire in ~15 minutes, so there is nothing to
# wait for past that.
TIMEOUT_SECONDS = 900
# Ink wraps at the terminal width; the token runs ~108 characters and has to
# land on one logical line for the extraction below to see all of it.
COLUMNS, ROWS = 250, 50
# Tokens are ~108 characters. Anything past this is not a token the TUI meant
# to print, and is reported rather than truncated to a plausible length.
MAX_TOKEN_LENGTH = 200

ANSI_CSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
AUTHORIZE_URL = re.compile(
    rb"https://[a-z.]+/[a-z/]*oauth/authorize\?[A-Za-z0-9%&=?:/._~+-]+"
)
# Deliberately open-ended: a ceiling here would match a long token's first N
# bytes, and `complete_match` would then see a following byte and call the
# truncation final. Length is checked after extraction so it fails loudly.
TOKEN = re.compile(rb"sk-ant-oat01-[A-Za-z0-9_-]{80,}")
# The TUI's own failure line. `claude setup-token` reports a rejected code and
# then waits at "Press Enter to retry", which produces no further output — so
# without this the run is silent to its deadline and reads as an approval the
# user never gave.
TUI_ERROR = re.compile(rb"OAuth\s*error:\s*([^\r\n]*)")
# Drives the progress line and nothing else. Gating the paste on it would tie
# the fallback path to TUI wording that is free to change, and buy nothing: the
# prompt renders alongside the authorize URL, so a code can't arrive first.
# Ink lays adjacent text nodes out with cursor moves rather than spaces, so
# stripping CSI closes the gaps between words: both patterns above read against
# the stripped view and have to tolerate their absence.
PASTE_PROMPT = re.compile(rb"Paste\s*code\s*here")


def complete_match(pattern, buf):
    """A match for `pattern` in `buf`, or None if it may still be growing.

    Output arrives in whatever chunks the pty hands over, so a match that runs
    to the end of the buffer can be the first half of a longer string that the
    next read completes. Requiring a byte after it makes the match final.
    """
    match = pattern.search(buf)
    if not match or match.end() == len(buf):
        return None
    return match.group(0)


def first_url(visible):
    """The authorize URL in ANSI-stripped output, or None.

    The TUI emits it as an OSC 8 hyperlink, whose target and visible text are
    the same URL back to back with no separator — so a match can run straight
    from the end of one into the start of the next.
    """
    url = complete_match(AUTHORIZE_URL, visible)
    if not url or b"state=" not in url:
        return None
    doubled = url.find(b"https://", 1)
    return url[:doubled] if doubled != -1 else url


def take_controlling_tty():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def failure_message(code_file, typed_at, announced, tui_error=None):
    """Why no token came back, in terms of what this run actually saw.

    The TUI's own error line beats every inference below it, so it comes
    first. A caller that already passed `--code-file` is told to pass it, and
    reads that as the diagnosis, so the causes it can act on are named apart.
    The loop ends three ways — the deadline, a pty the child closed, and a
    child already exited — so a message may not assume the window passed
    either. A run that never announced a URL ended inside `claude setup-token`,
    whose own output this wrapper swallows, and is the one case where the fix
    is to go look at that command rather than at the browser.
    """
    if tui_error:
        return (
            f"Error: `claude setup-token` reported: {tui_error}\n"
            "A code belongs to the run whose challenge issued it, is good once, "
            "and dies with that run — so a code from an earlier run, or one the "
            "CLI's own localhost callback already redeemed, fails here. Rerun "
            "and approve the fresh URL."
        )
    if typed_at is not None:
        return (
            f"Error: the code from {code_file} was typed into the prompt and no "
            "token came back, and the TUI reported no error either. Rerun and "
            "approve the fresh URL."
        )
    if not announced:
        return (
            "Error: `claude setup-token` ended without offering an authorize "
            "URL, so it failed before the browser was ever involved. Its output "
            "is not echoed here — the TUI carries the token — so run that "
            "command directly to see what it said."
        )
    if not code_file:
        return (
            "Error: no sk-ant-oat01-… token in TUI output. If the browser showed "
            "a code to copy, rerun with --code-file and write it to that path."
        )
    return (
        f"Error: the authorize URL went unapproved — nothing was written to "
        f"{code_file} and the browser never came back to the CLI. This run "
        f"waits up to {TIMEOUT_SECONDS}s for an approval, which takes a person "
        "at the browser throughout; a rerun issues a fresh URL that replaces "
        "this one."
    )


def stop_and_reap(child, terminate_timeout=10):
    """Stop a child's process group and reap its group leader."""
    process_group = child.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return

    deadline = time.monotonic() + terminate_timeout
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            pass
        time.sleep(0.01)
    else:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
        while True:
            child.poll()
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                pass
            time.sleep(0.01)

    if child.returncode is None:
        child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-file", help="path to watch for a `code#state` string")
    args = parser.parse_args()

    def stop_on_signal(signum, _frame):
        for interrupt in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(interrupt, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGHUP, stop_on_signal)
    signal.signal(signal.SIGINT, stop_on_signal)
    signal.signal(signal.SIGTERM, stop_on_signal)

    # A code from an earlier run is dead — its PKCE challenge went with that
    # run — so a leftover file would be typed into this run's prompt before the
    # user has approved anything, and the run would wait out its whole deadline.
    if args.code_file and os.path.exists(args.code_file):
        os.unlink(args.code_file)

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
    try:
        child = subprocess.Popen(
            ["claude", "setup-token"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            # The hazard is threads; this is a single-threaded CLI helper, and the
            # child needs the pty as its controlling terminal to run interactively.
            preexec_fn=take_controlling_tty,  # noqa: PLW1509
        )
    except FileNotFoundError:
        sys.exit("Error: claude CLI not found. Install Claude Code first.")
    os.close(slave)

    buf = bytearray()
    announced = False
    prompted = False
    typed_at = None
    enters = 0
    token = None
    deadline = time.time() + TIMEOUT_SECONDS

    try:
        while time.time() < deadline:
            readable, _, _ = select.select([master], [], [], 1.0)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # the child closed its end
                    chunk = b""
                if not chunk:
                    break
                buf += chunk

            # Ink restyles mid-frame, so a CSI sequence can land inside either the
            # URL or the token. Both are read from the stripped view.
            visible = ANSI_CSI.sub(b"", buf)

            if not announced:
                url = first_url(visible)
                if url:
                    print(f"Approve in the browser:\n{url.decode()}", file=sys.stderr)
                    announced = True

            if not prompted and PASTE_PROMPT.search(visible):
                prompted = True
                fallback = (
                    f"If it shows a `code#state` string instead, write that to "
                    f"{args.code_file}."
                    if args.code_file
                    else "If it shows a `code#state` string instead, this run has "
                    "no --code-file to read it from."
                )
                print(
                    "Waiting for the approval. The CLI takes the redirect on its "
                    "own localhost callback, so approving in the browser is "
                    f"usually the whole job. {fallback}",
                    file=sys.stderr,
                )

            if typed_at is None and args.code_file and os.path.exists(args.code_file):
                with open(args.code_file, encoding="utf-8") as fh:
                    code = fh.read().strip()
                if code:
                    os.write(master, code.encode())
                    typed_at = time.time()

            # Enter is its own write, a beat behind the code: a code and a newline
            # arriving together are buffered as one paste and the newline never
            # lands as a keypress. Repeated in case the first arrives while the TUI
            # is still settling the paste.
            if (
                typed_at is not None
                and enters < 3
                and time.time() - typed_at > 1.5 + 3 * enters
            ):
                os.write(master, b"\r")
                enters += 1

            match = complete_match(TOKEN, visible)
            if match:
                token = match.decode()
                break

            # After the token check, so a token already on screen wins over an
            # error earlier in the same buffer. The message itself is read off
            # the final buffer below, where the line has finished arriving.
            if TUI_ERROR.search(visible):
                break

            if child.poll() is not None and not readable:
                break
    finally:
        try:
            stop_and_reap(child)
        finally:
            os.close(master)

    final_visible = ANSI_CSI.sub(b"", buf)
    if token is None:
        # Nothing more can arrive, so a match here cannot be a partial read —
        # which is the case where the token is the last thing the TUI writes.
        final = TOKEN.search(final_visible)
        if final:
            token = final.group(0).decode()

    if not token:
        # Read unconditionally rather than off a flag the loop set: the loop
        # also ends on a closed pty and on an exited child, either of which can
        # carry the error line in the same read that ends it.
        error = TUI_ERROR.search(final_visible)
        detail = error.group(1).decode("utf-8", "replace").strip() if error else None
        sys.exit(failure_message(args.code_file, typed_at, announced, detail))
    if len(token) > MAX_TOKEN_LENGTH:
        sys.exit(f"Error: extracted token has implausible length ({len(token)} chars)")
    print(token)


if __name__ == "__main__":
    main()
