# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Select today's deterministic slice of tracked files for the nightly survey.

Both the day and the path pick a bucket by arithmetic, so bucket *N* returns to
the same file set every cycle. On a repository whose pull requests sit unmerged
longer than that, a path whose last pass left an open PR comes round again and
its findings get re-derived on the anniversary. So each path an open pull
request already changes is reported with the pulls covering it and who wrote
them, for the survey to read before it reads the file. The author decides where
a new finding goes: onto the covering pull request where the bot wrote it, into
a pull request of its own where anyone else did. The path stays in the list: on
the repositories where this fires the backlog can cover most of the tree, and
dropping those paths would leave them unsurveyed for as long as it stands.
"""

from __future__ import annotations

import subprocess
import sys
import time
import zlib
from typing import Any

import github_cli

CYCLE_LENGTH = 28


def survey_files(files: list[str], *, unix_day: int) -> tuple[int, list[str]]:
    """Return the day's bucket and the files assigned to it."""
    bucket = unix_day % CYCLE_LENGTH
    selected = [
        path for path in files if zlib.crc32(path.encode()) % CYCLE_LENGTH == bucket
    ]
    return bucket, selected


def covering_pulls(pull_requests: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map each path an open pull request changes to the pulls changing it."""
    covering: dict[str, list[str]] = {}
    for pull in pull_requests:
        label = f"#{pull['number']} {pull['author']['login']}"
        for changed in pull["files"]:
            covering.setdefault(changed["path"], []).append(label)
    return covering


def main(
    argv: list[str] | None = None,
    *,
    now: float | None = None,
    pull_requests: list[dict[str, Any]] | None = None,
) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print(f"usage: {sys.argv[0]}", file=sys.stderr)
        return 2
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    bucket, selected = survey_files(
        tracked, unix_day=int(time.time() if now is None else now) // 86400
    )
    if pull_requests is None:
        pull_requests = github_cli.json_call(
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,files,author",
        )
    covering = covering_pulls(pull_requests)
    covered = [path for path in selected if path in covering]
    if selected:
        print(*selected, sep="\n")
    print(
        f"# bucket={bucket}/{CYCLE_LENGTH} files={len(selected)}"
        f" covered={len(covered)}",
        file=sys.stderr,
    )
    for path in covered:
        print(f"# covered {path} ({', '.join(covering[path])})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error))
