from __future__ import annotations

import re
from pathlib import Path

import compose_system_prompt
import pytest


def test_composes_directives_shared_prompt_and_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "system-prompt.md"
    output = tmp_path / "github-output"
    shared.write_text("Act as **${BOT_NAME}**. Keep `$GH_TOKEN` intact.\n")
    monkeypatch.setenv("SYSTEM_PROMPT_FILE", str(shared))
    monkeypatch.setenv("BOT_NAME", "tend-bot")
    monkeypatch.setenv("EXTRA", "One more rule.")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert compose_system_prompt.main() == 0

    written = output.read_text()
    match = re.fullmatch(r"value<<(tend-[0-9a-f]+)\n(.*)\n\1\n", written, re.DOTALL)
    assert match
    assert match.group(2) == (
        f"{compose_system_prompt.CLAUDE_DIRECTIVE}\n\n"
        f"{compose_system_prompt.AUTONOMY_DIRECTIVE}\n\n"
        "Act as **tend-bot**. Keep `$GH_TOKEN` intact.\n\n"
        "One more rule."
    )
