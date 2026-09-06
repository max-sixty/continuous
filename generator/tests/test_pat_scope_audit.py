"""Behavior tests for pat_scope_audit.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import GH_PREAMBLE, fake_bin, tool_path, uv_script

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "tend-ci-runner"
    / "scripts"
    / "pat_scope_audit.py"
)

FAKE_GH = (
    GH_PREAMBLE
    + r"""
if [ -n "${GH_FAILS:-}" ]; then
  echo "gh failed" >&2
  exit 17
fi
cat "$HEADERS"
"""
)


def _run(tmp_path: Path, headers: str, *args: str, fails: bool = False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    header_file = tmp_path / "headers"
    header_file.write_text(headers)
    bindir = fake_bin(tmp_path, gh=FAKE_GH)
    return subprocess.run(
        uv_script(SCRIPT, *args),
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": tool_path(bindir),
            "HOME": str(tmp_path),
            "GH_CALLS": str(tmp_path / "gh-calls"),
            "HEADERS": str(header_file),
            "GH_FAILS": "1" if fails else "",
        },
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            (
                "HTTP/2 200\nX-OAuth-Scopes: repo, workflow, notifications, "
                "write:discussion, gist, user\n\n{}\n"
            ),
            (
                "STATUS=ok\n"
                "GRANTED=repo,workflow,notifications,write:discussion,gist,user\n"
                "REQUIRED=repo,workflow,notifications,write:discussion,gist,user\n"
                "MISSING=\n"
            ),
        ),
        (
            "HTTP/2 200\nx-oauth-scopes: repo, workflow, gist\n\n{}\n",
            (
                "STATUS=missing\n"
                "GRANTED=repo,workflow,gist\n"
                "REQUIRED=repo,workflow,notifications,write:discussion,gist,user\n"
                "MISSING=notifications,write:discussion,user\n"
            ),
        ),
        ("HTTP/2 200\ncontent-type: application/json\n\n{}\n", "STATUS=fine-grained\n"),
    ],
)
def test_scope_headers_produce_the_skill_contract(
    tmp_path: Path, headers: str, expected: str
) -> None:
    result = _run(tmp_path, headers)

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_usage_and_gh_failures_are_not_reported_as_scope_results(
    tmp_path: Path,
) -> None:
    usage = _run(tmp_path / "usage", "", "unexpected")
    assert usage.returncode == 2
    assert usage.stdout == ""

    failed = _run(tmp_path / "failure", "", fails=True)
    assert failed.returncode == 17
    assert failed.stdout == ""
    assert "gh failed" in failed.stderr
