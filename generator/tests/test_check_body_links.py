"""Tests for check_body_links.py.

The script exists because shape and existence are different questions: a
hand-typed 40-hex string is a well-formed permalink whether or not the commit
is real, so the pre-post scan it replaces passed a fabricated OID straight
through to a public PR body. The fake `gh` therefore resolves exactly one
(repo, SHA) pair and refuses everything else, which is what an invented OID
and a wrong owner both look like from the API.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import GH_PREAMBLE, fake_bin, tool_path, uv_script

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts" / "check_body_links.py"

SLUG = "max-sixty/tend"
REAL = "0123456789abcdef0123456789abcdef01234567"
FAKE = "0123456789abcdefdeadbeefdeadbeefdeadbeef"

# Only the one commit in the one repo resolves; every other path 404s, the way
# an extended-abbreviation OID and a hand-typed owner both do.
FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$*" in
  "api repos/"""
    + SLUG
    + "/commits/"
    + REAL
    + r""" "*) emit '{"sha": "'"""
    + REAL
    + r"""'"}' ;;
  *) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
esac
"""
)


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls = tmp_path / "gh-calls"
        self.bindir = fake_bin(tmp_path, gh=FAKE_GH)

    def run(self, body: str) -> subprocess.CompletedProcess[str]:
        path = self.tmp_path / "body.md"
        path.write_text(body)
        return self.run_path(str(path))

    def run_path(self, arg: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            uv_script(SCRIPT, arg, *extra),
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": tool_path(self.bindir),
                "HOME": str(self.tmp_path),
                "GH_CALLS": str(self.calls),
            },
        )

    def gh_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [ln for ln in self.calls.read_text().splitlines() if ln]


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_resolvable_permalink_and_file_level_branch_link_pass(fx: Fixture) -> None:
    """A pinned line link that resolves, and a branch link with no line anchor."""
    result = fx.run(
        f"See [decorators.py#L305-L331](https://github.com/{SLUG}/blob/{REAL}/a.py#L305-L331) "
        f"and https://github.com/{SLUG}/blob/main/CLAUDE.md for context.\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_fabricated_sha_is_reported(fx: Fixture) -> None:
    """The shape is valid; only resolving the OID tells the two apart."""
    result = fx.run(f"[a.py#L10](https://github.com/{SLUG}/blob/{FAKE}/a.py#L10)\n")
    assert result.returncode == 1
    assert FAKE in result.stdout
    assert "unresolvable SHA" in result.stdout


def test_wrong_owner_is_reported_by_the_same_check(fx: Fixture) -> None:
    result = fx.run(f"https://github.com/anthropics/tend/blob/{REAL}/a.py#L10\n")
    assert result.returncode == 1
    assert "anthropics/tend" in result.stdout


def test_branch_pinned_line_anchor_is_reported(fx: Fixture) -> None:
    result = fx.run(f"https://github.com/{SLUG}/blob/main/CLAUDE.md#L10\n")
    assert result.returncode == 1
    assert "un-pinned line link" in result.stdout
    assert fx.gh_calls() == []


def test_abbreviated_sha_with_a_line_anchor_is_reported(fx: Fixture) -> None:
    """An abbreviation is what the model extends; it cannot pin a line either."""
    result = fx.run(f"https://github.com/{SLUG}/blob/{REAL[:7]}/a.py#L10\n")
    assert result.returncode == 1
    assert "un-pinned line link" in result.stdout


def test_each_distinct_sha_costs_one_api_call(fx: Fixture) -> None:
    """Bodies repeat the same permalink; the check must not repeat the call."""
    body = "\n".join(
        [
            f"https://github.com/{SLUG}/blob/{REAL}/a.py#L1",
            f"https://github.com/{SLUG}/blob/{REAL}/b.py#L2",
            f"https://github.com/{SLUG}/commit/{FAKE}",
        ]
    )
    result = fx.run(body + "\n")
    assert result.returncode == 1
    assert len(fx.gh_calls()) == 2


def test_markdown_delimiters_do_not_leak_into_the_ref(fx: Fixture) -> None:
    """A URL closed by `)` or wrapped in backticks still parses to the bare ref."""
    result = fx.run(
        f"([a](https://github.com/{SLUG}/blob/{FAKE}/a.py#L10)) "
        f"`https://github.com/{SLUG}/blob/{FAKE}/b.py`\n"
    )
    assert result.returncode == 1
    assert f"unresolvable SHA {FAKE} in {SLUG}" in result.stdout
    assert ")" not in result.stdout.split(" in ")[0]


def test_sentence_punctuation_does_not_skip_the_resolve(fx: Fixture) -> None:
    """A bare `commit/<sha>` URL in prose ends at the period, which is part of the ref."""
    result = fx.run(
        f"Pushed as https://github.com/{SLUG}/commit/{FAKE}.\n"
        f"See https://github.com/{SLUG}/commit/{REAL}, which is real.\n"
    )
    assert result.returncode == 1
    assert f"unresolvable SHA {FAKE} in {SLUG}" in result.stdout
    assert REAL not in result.stdout


def test_body_without_github_links_passes_without_calling_the_api(fx: Fixture) -> None:
    result = fx.run("Thanks for reporting this — I could not reproduce it.\n")
    assert result.returncode == 0
    assert fx.gh_calls() == []


def test_missing_file_and_bad_arity_exit_two(fx: Fixture) -> None:
    missing = fx.run_path(str(fx.tmp_path / "nope.md"))
    assert missing.returncode == 2
    assert "no such file" in missing.stderr

    extra = fx.run_path(str(fx.tmp_path), "second")
    assert extra.returncode == 2
    assert "usage:" in extra.stderr
