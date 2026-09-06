"""Behavior tests for Tend's repo-local consumer index updater."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests import GH_PREAMBLE, fake_bin, tool_path, uv_script

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "running-tend"
    / "scripts"
    / "refresh_consumers.py"
)

FAKE_GH = (
    GH_PREAMBLE
    + r"""
case "$1 $2" in
  "search code") emit "$(cat "$SEARCH_JSON")" ;;
  "api repos/a/new/contents/.github/workflows")
    emit '[{"name":"tend-review.yaml"}]' ;;
  "api repos/a/new/contents/.config/tend.yaml")
    emit '{"content":"Ym90X25hbWU6IG5ldy1ib3QK"}' ;;
  "api repos/w/config-missing/contents/.github/workflows")
    emit '[{"name":"tend-review.yaml"}]' ;;
  "api repos/w/config-missing/contents/.config/tend.yaml")
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
    ;;
  "api repos/x/missing/contents/.github/workflows")
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
    ;;
  "api repos/y/removed/contents/.github/workflows")
    emit '[{"name":"ci.yaml"}]' ;;
  "api repos/z/retained/contents/.github/workflows")
    emit '[{"name":"tend-nightly.yaml"}]' ;;
  "api repos/z/retained/contents/.config/tend.yaml")
    emit '{"content":"Ym90X25hbWU6IGN1cnJlbnQtYm90Cg=="}' ;;
  "api repos/broken/repo/contents/.github/workflows")
    echo "GitHub API unavailable" >&2
    exit 17
    ;;
  *) exit 1 ;;
esac
"""
)


def environment(tmp_path: Path, search: list[dict[str, object]]) -> dict[str, str]:
    """Install a fake GitHub CLI backed by one code-search response."""
    search_path = tmp_path / "search.json"
    search_path.write_text(json.dumps(search))
    bindir = fake_bin(tmp_path, gh=FAKE_GH)
    return {
        "PATH": tool_path(bindir),
        "HOME": str(tmp_path),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "SEARCH_JSON": str(search_path),
    }


def test_refresh_unions_search_with_the_index_and_confirms_removals(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    consumers = data / "consumers.json"
    consumers.write_text(
        json.dumps(
            [
                {"repo": "w/config-missing", "bot_name": "old-bot"},
                {"repo": "x/missing", "bot_name": "old-bot"},
                {"repo": "y/removed", "bot_name": "old-bot"},
                {"repo": "z/retained", "bot_name": "stale-name"},
            ],
            indent=2,
        )
        + "\n"
    )
    search = [
        {
            "repository": {"nameWithOwner": "a/new"},
            "path": ".github/workflows/tend-review.yaml",
        },
        {
            "repository": {"nameWithOwner": "irrelevant/repo"},
            "path": "README.yaml",
        },
    ]

    result = subprocess.run(
        uv_script(SCRIPT),
        cwd=tmp_path,
        env=environment(tmp_path, search),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "changed": True,
        "discovered": 1,
        "consumers": 2,
        "removed": ["w/config-missing", "x/missing", "y/removed"],
    }
    assert json.loads(consumers.read_text()) == [
        {"repo": "a/new", "bot_name": "new-bot"},
        {"repo": "z/retained", "bot_name": "current-bot"},
    ]
    calls = (tmp_path / "gh-calls.log").read_text()
    assert (
        "search code max-sixty/tend --extension yaml --limit 100 --json repository,path"
    ) in calls
    assert "irrelevant/repo" not in calls
    assert "repos/w/config-missing/contents/.config/tend.yaml" in calls
    assert "repos/x/missing/contents/.config/tend.yaml" not in calls
    assert "repos/y/removed/contents/.config/tend.yaml" not in calls


def test_github_failure_leaves_the_existing_index_untouched(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    consumers = data / "consumers.json"
    original = '[{"repo":"broken/repo","bot_name":"bot"}]\n'
    consumers.write_text(original)

    result = subprocess.run(
        uv_script(SCRIPT),
        cwd=tmp_path,
        env=environment(tmp_path, []),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 17
    assert "GitHub API unavailable" in result.stderr
    assert consumers.read_text() == original


def test_weekly_skill_delegates_the_refresh_to_the_script() -> None:
    skill = SCRIPT.parent.parent / "SKILL.md"
    content = skill.read_text()

    assert "scripts/refresh_consumers.py" in content
    assert "mapfile" not in content
