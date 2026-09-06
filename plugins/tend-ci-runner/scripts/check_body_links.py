# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Validate GitHub source links in a composed body before publication.

Line anchors must use a full commit SHA, and each full SHA must resolve in the
repository named by its URL. A local commit therefore fails until it is pushed,
which is the same point at which the link becomes readable by its audience.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import github_cli

URL_RE = re.compile(
    r"https?://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/"
    r"(?:blob|blame|tree|commit|commits|raw)/[^][)(\"<>`'\s]+"
)
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def check_links(body: str) -> list[str]:
    """Return publication-blocking problems in *body*."""
    problems: list[str] = []
    commits: set[tuple[str, str]] = set()

    for match in URL_RE.finditer(body):
        url = match.group()
        parts = url.split("/")
        slug = "/".join(parts[3:5])
        ref = parts[6].split("#", 1)[0].rstrip(".,;:!?")
        if FULL_SHA_RE.fullmatch(ref):
            commits.add((slug, ref))
        elif "#L" in url:
            problems.append(
                f"un-pinned line link: ref `{ref}` is not a full commit SHA, "
                f"so the lines it points at can move — {url}"
            )

    for slug, sha in sorted(commits):
        try:
            github_cli.run(
                "api", f"repos/{slug}/commits/{sha}", "--jq", ".sha", quiet=True
            )
        except subprocess.CalledProcessError:
            problems.append(
                f"unresolvable SHA {sha} in {slug} — the commit does not exist "
                "(a hand-typed OID or a wrong owner), or the token cannot read "
                "that repo"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(f"usage: {sys.argv[0]} <body-file>", file=sys.stderr)
        return 2
    body_path = Path(args[0])
    if not body_path.is_file():
        print(f"{Path(sys.argv[0]).name}: no such file: {body_path}", file=sys.stderr)
        return 2

    problems = check_links(body_path.read_text())
    if problems:
        print(*problems, sep="\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
