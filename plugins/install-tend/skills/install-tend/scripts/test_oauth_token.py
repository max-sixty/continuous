"""Tests for the OAuth wrapper's extractors.

Run: ``uv run pytest`` from the repo root, which covers every Python suite.

`complete_match` and `first_url` are pure functions over whatever bytes the pty
happens to deliver, and every way they can be wrong ends the same way: a
credential that is stored and authenticates nothing, or a run that discards a
token after an approval has already been spent. Nothing downstream re-checks
them, so they are pinned here.
"""

from __future__ import annotations

from oauth_token import ANSI_CSI, AUTHORIZE_URL, MAX_TOKEN_LENGTH, TOKEN
from oauth_token import complete_match, first_url

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
