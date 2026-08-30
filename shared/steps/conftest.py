"""Fixtures for the step-body tests in this directory.

``fake_gh`` replaces ``_common.gh`` for the test, so a step's GitHub calls hit
canned responses and are recorded, and a test never puts a shim on ``PATH``.
``github_files`` points the runner's file channels (``GITHUB_OUTPUT``,
``GITHUB_ENV``, ``GITHUB_STEP_SUMMARY``) at files under ``tmp_path``.
``actions_env`` sets the run and event variables a step reads to name itself.
The doubles themselves live in ``_fakes.py`` so tests can import their types.
"""

from __future__ import annotations

import json
from pathlib import Path

import _common
import pytest
from _fakes import FakeGh, GithubFiles


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(_common, "gh", fake)
    return fake


@pytest.fixture
def github_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GithubFiles:
    files = GithubFiles(tmp_path / "output", tmp_path / "env", tmp_path / "summary")
    for path in (files.output, files.env, files.summary):
        path.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(files.output))
    monkeypatch.setenv("GITHUB_ENV", str(files.env))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(files.summary))
    return files


@pytest.fixture
def actions_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Actions run/event variables, with a pull_request event on disk.

    Returns the event payload's path, so a test that needs a different trigger
    rewrites the file and resets ``GITHUB_EVENT_NAME``.
    """
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 851}}))
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    return event
