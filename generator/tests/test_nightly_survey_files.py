"""Tests for nightly_survey_files.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
import zlib
from pathlib import Path
from types import ModuleType

import pytest

from tests import GH_PREAMBLE, fake_bin, tool_path, uv_script

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "tend-ci-runner"
    / "scripts"
    / "nightly_survey_files.py"
)


@pytest.fixture(scope="module")
def survey() -> ModuleType:
    spec = importlib.util.spec_from_file_location("nightly_survey_files", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # `uv run --script` puts the script's own directory on the path; an
    # in-process import of its `github_cli` sibling needs the same.
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPT.parent))
    return module


def test_daily_selection_assigns_each_path_to_one_stable_bucket(
    survey: ModuleType,
) -> None:
    files = [f"file-{index}.py" for index in range(100)]
    bucket, selected = survey.survey_files(files, unix_day=10_000)

    assert bucket == 4
    assert selected
    selections = [
        survey.survey_files(files, unix_day=day)[1]
        for day in range(survey.CYCLE_LENGTH)
    ]
    assert sorted(path for paths in selections for path in paths) == sorted(files)


def test_main_lists_the_tracked_files_in_the_fixed_days_bucket(
    survey: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    files = [f"file-{index}.py" for index in range(100)]
    for path in files:
        (tmp_path / path).write_text("\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)

    assert survey.main([], now=10_000 * 86400, pull_requests=[]) == 0

    captured = capsys.readouterr()
    bucket, selected = survey.survey_files(sorted(files), unix_day=10_000)
    assert captured.out.splitlines() == selected
    assert captured.err == f"# bucket={bucket}/28 files={len(selected)} covered=0\n"


def test_paths_an_open_pr_already_changes_stay_listed_and_are_named(
    survey: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    files = [f"file-{index}.py" for index in range(100)]
    for path in files:
        (tmp_path / path).write_text("\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    _, selected = survey.survey_files(sorted(files), unix_day=10_000)
    covered = selected[0]

    assert (
        survey.main(
            [],
            now=10_000 * 86400,
            pull_requests=[
                {"number": 7, "files": [{"path": covered}]},
                {"number": 9, "files": [{"path": covered}, {"path": "untracked.py"}]},
            ],
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out.splitlines() == selected
    assert captured.err == (
        f"# bucket=4/28 files={len(selected)} covered=1\n# covered {covered} (#7, #9)\n"
    )


def test_the_days_list_names_the_open_prs_gh_reports(
    survey: ModuleType, tmp_path: Path
) -> None:
    """The whole `gh` surface, on whichever bucket today's real clock picks."""
    today = int(time.time()) // 86400 % survey.CYCLE_LENGTH
    in_bucket = [
        name
        for name in (f"file-{index}.py" for index in range(2000))
        if zlib.crc32(name.encode()) % survey.CYCLE_LENGTH == today
    ]
    covered, kept = in_bucket[0], in_bucket[1]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for path in (covered, kept):
        (tmp_path / path).write_text("\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    calls = tmp_path / "gh-calls"
    bindir = fake_bin(
        tmp_path,
        gh=GH_PREAMBLE
        + f"""emit '[{{"number": 12, "files": [{{"path": "{covered}"}}]}}]'\n""",
    )

    result = subprocess.run(
        uv_script(SCRIPT),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": tool_path(bindir), "HOME": str(tmp_path), "GH_CALLS": str(calls)},
        check=True,
    )

    assert sorted(result.stdout.splitlines()) == sorted([covered, kept])
    assert f"# covered {covered} (#12)" in result.stderr
    assert calls.read_text().splitlines() == [
        "pr list --state open --limit 200 --json number,files"
    ]
