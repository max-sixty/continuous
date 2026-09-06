"""The Codex surface probe driven through one fake external CLI."""

from __future__ import annotations

from pathlib import Path

import codex_surface
import pytest


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "triage").mkdir(parents=True)
    (plugin / "skills" / "triage" / "SKILL.md").write_text("# Triage\n")
    (plugin / "scripts").mkdir()
    script = plugin / "scripts" / "list_recent_runs.py"
    script.write_text("#!/usr/bin/env python3\n")

    executable = tmp_path / "codex"
    executable.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["login", "status"]:
    if os.environ.get("FAKE_REWRITE_AUTH"):
        Path(os.environ["CODEX_HOME"], "auth.json").write_text("rewritten\\n")
    print("Logged in using ChatGPT")
elif args == ["exec", "--help"]:
    print("--model <MODEL> --sandbox <MODE> --output-last-message <FILE> --config <KEY=VALUE> --json --skip-git-repo-check")
elif args[:3] == ["plugin", "marketplace", "add"]:
    pass
elif args == ["plugin", "add", "tend-ci-runner@tend"]:
    print(f"Installed plugin root: {os.environ['FAKE_PLUGIN_ROOT']}")
elif args[:3] == ["exec", "--strict-config", "-c"]:
    if "ZZZ" in args[3]:
        print("unknown configuration field")
    else:
        print("Not inside a trusted directory")
    sys.exit(1)
else:
    print(f"unexpected arguments: {args}", file=sys.stderr)
    sys.exit(2)
"""
    )
    executable.chmod(0o755)
    monkeypatch.setenv("FAKE_PLUGIN_ROOT", str(plugin))
    return executable


def test_verify_exercises_the_whole_codex_contract(
    tmp_path: Path, fake_codex: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    codex_surface.verify(tmp_path, str(fake_codex))

    assert "Installed plugin root:" in capsys.readouterr().out


def test_verify_rejects_a_cli_that_rewrites_consumer_auth(
    tmp_path: Path, fake_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_REWRITE_AUTH", "1")

    with pytest.raises(codex_surface.SurfaceError, match="rewrote access-only"):
        codex_surface.verify(tmp_path, str(fake_codex))
