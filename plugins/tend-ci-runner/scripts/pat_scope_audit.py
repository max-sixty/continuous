# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Report whether the authenticated bot's classic PAT has Tend's scopes."""

from __future__ import annotations

import subprocess
import sys

import github_cli

REQUIRED = ("repo", "workflow", "notifications", "write:discussion", "gist", "user")


def audit(headers: str) -> dict[str, str]:
    """Parse GitHub response headers into the skill's key-value result."""
    scopes_line = next(
        (
            line.partition(":")[2]
            for line in headers.splitlines()
            if line.casefold().startswith("x-oauth-scopes:")
        ),
        None,
    )
    if scopes_line is None:
        return {"STATUS": "fine-grained"}

    granted = [scope.strip() for scope in scopes_line.split(",") if scope.strip()]
    missing = [scope for scope in REQUIRED if scope not in granted]
    result = {
        "STATUS": "missing" if missing else "ok",
        "GRANTED": ",".join(granted),
        "REQUIRED": ",".join(REQUIRED),
        "MISSING": ",".join(missing),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print(f"usage: {sys.argv[0]}", file=sys.stderr)
        return 2
    result = audit(github_cli.run("api", "-i", "user"))
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
