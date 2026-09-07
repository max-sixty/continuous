"""Tests for nightly_survey_files.py."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

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
    spec.loader.exec_module(module)
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

    assert survey.main([], now=10_000 * 86400) == 0

    captured = capsys.readouterr()
    bucket, selected = survey.survey_files(sorted(files), unix_day=10_000)
    assert captured.out.splitlines() == selected
    assert captured.err == f"# bucket={bucket}/28 files={len(selected)}\n"
