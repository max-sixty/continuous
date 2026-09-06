# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Select today's deterministic slice of tracked files for the nightly survey."""

from __future__ import annotations

import subprocess
import sys
import time
import zlib

CYCLE_LENGTH = 28


def survey_files(files: list[str], *, unix_day: int) -> tuple[int, list[str]]:
    """Return the day's bucket and the files assigned to it."""
    bucket = unix_day % CYCLE_LENGTH
    selected = [
        path for path in files if zlib.crc32(path.encode()) % CYCLE_LENGTH == bucket
    ]
    return bucket, selected


def main(argv: list[str] | None = None, *, now: float | None = None) -> int:
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
    if selected:
        print(*selected, sep="\n")
    print(f"# bucket={bucket}/{CYCLE_LENGTH} files={len(selected)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode or 1) from None
