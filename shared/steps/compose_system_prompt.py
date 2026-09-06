"""Compose the Claude harness's system prompt and emit it as a step output."""

from __future__ import annotations

import os
from pathlib import Path

import _common

CLAUDE_DIRECTIVE = "Use /tend-ci-runner:running-in-ci before starting work."
AUTONOMY_DIRECTIVE = (
    "You are running in CI; no human is available to answer questions. Never "
    "prompt for clarification or approval. When uncertain, make the best "
    "reasonable choice from the available evidence and proceed. Permissions "
    "are pre-approved; tool calls execute without confirmation."
)


def _substitute_bot_name(text: str, bot_name: str) -> str:
    return text.replace("${BOT_NAME}", bot_name).replace("$BOT_NAME", bot_name)


def main() -> int:
    shared = Path(os.environ["SYSTEM_PROMPT_FILE"]).read_text()
    base = _substitute_bot_name(shared, os.environ["BOT_NAME"]).rstrip("\n")
    parts = [CLAUDE_DIRECTIVE, AUTONOMY_DIRECTIVE, base]
    extra = os.environ.get("EXTRA", "")
    if extra:
        parts.append(extra)
    _common.set_output("value", "\n\n".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
