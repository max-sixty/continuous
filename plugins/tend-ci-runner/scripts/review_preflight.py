# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Bind a review post to the open PR head the session actually reviewed.

The command prints ``post:`` when the outward action may proceed and ``skip:``
when it must stop. If a compatible push moved the head, it also writes the
incremental diff to a persistent temporary file and prints its path. Passing a
command after ``--`` makes the check the final posting boundary; that form
never retargets and propagates the command's exit status.

``start`` records one consistent initial snapshot and prepares the prior-review
incremental, while ``post`` protects the final outward action. Keeping both in
one review-domain command makes the state passed between them explicit without
turning unrelated runner helpers into a single application.

Run this script directly with ``/usr/bin/python3 -E -s``, not through ``uv``.
The reviewed-head pin advances only after the status reaches stdout; ``uv run``
reopens a closed stdout for its child and would hide a failed delivery. The
action's system interpreter is the same isolated standard-library runtime used
by its shared step bodies, so this module and its imports stay Python 3.10+
compatible for supported runner overrides.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import bot_review_state
import github_cli

SHA_RE = re.compile(r"[0-9a-f]{40}")


def _error(message: str) -> int:
    print(f"review-preflight: {message}", file=sys.stderr)
    return 1


def _skip(message: str) -> int:
    print(f"skip: {message}")
    return 0


def _pr_view(pr: str, repo: str, fields: str) -> dict[str, Any] | None:
    try:
        result = github_cli.json_call(
            "pr", "view", pr, "--repo", repo, "--json", fields
        )
    except (json.JSONDecodeError, subprocess.CalledProcessError):
        return None
    return result if isinstance(result, dict) else None


def _event_forces_review() -> bool:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return False
    try:
        event = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(event, dict) and event.get("action") == "ready_for_review"


def _write_delta(reviewed: str, current_head: str, base_sha: str) -> tuple[int, str]:
    commands = (
        (
            "log",
            "-p",
            "--no-merges",
            "--format=%h %s",
            f"{reviewed}..{current_head}",
            "--not",
            base_sha,
        ),
        (
            "log",
            "--format=base merge: %h %s",
            "--merges",
            f"{reviewed}..{current_head}",
        ),
    )
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as delta:
        path = delta.name
        for command in commands:
            result = subprocess.run(
                ["git", *command], stdout=delta, text=True, check=False
            )
            if result.returncode:
                return result.returncode, path
    return 0, path


def _write_incremental(
    reviewed: str, current_head: str, base_sha: str
) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as incremental:
        path = incremental.name
        result = subprocess.run(
            [
                "git",
                "log",
                "--no-merges",
                "--numstat",
                "--format=%h %s",
                f"{reviewed}..{current_head}",
                "--not",
                base_sha,
            ],
            stdout=incremental,
            text=True,
            check=False,
        )
    return result.returncode, path


def _emit_json(value: dict[str, Any]) -> bool:
    try:
        json.dump(value, sys.stdout, indent=2, separators=(",", ": "))
        sys.stdout.write("\n")
        sys.stdout.flush()
    except OSError:
        return False
    return True


def _start(pr: str) -> int:
    repo = github_cli.repository()
    initial = _pr_view(pr, repo, "headRefOid,state,baseRefOid,author,isDraft")
    if not initial or not all(
        initial.get(key) is not None
        for key in ("headRefOid", "state", "baseRefOid", "author", "isDraft")
    ):
        return _error(f"could not read PR #{pr}")

    head_sha = str(initial["headRefOid"])
    state = str(initial["state"])
    if state != "OPEN":
        return _skip(f"PR is {state}")
    base_sha = str(initial["baseRefOid"])
    author = initial["author"]
    if not isinstance(author, dict) or not author.get("login"):
        return _error(f"could not read PR #{pr} author")

    review_state = bot_review_state.fetch_review_state(pr, repo=repo)
    if review_state.get("head_sha") != head_sha:
        return _error(
            f"PR #{pr} head moved during the initial snapshot; run start again"
        )

    force_full_review = _event_forces_review()
    force_pushed = bool(review_state.get("force_pushed_since"))
    last_review_sha = str((review_state.get("last_substantive") or {}).get("sha") or "")
    incremental_path: str | None = None
    if (
        last_review_sha
        and last_review_sha != head_sha
        and not force_full_review
        and not force_pushed
    ):
        subprocess.run(
            ["git", "fetch", "--no-tags", "--quiet", "origin", f"refs/pull/{pr}/head"],
            check=False,
        )
        subprocess.run(
            ["git", "fetch", "--no-tags", "--quiet", "origin", base_sha],
            check=False,
        )
        status, incremental_path = _write_incremental(
            last_review_sha, head_sha, base_sha
        )
        if status:
            return status

    context = {
        "head_sha": head_sha,
        "author": str(author["login"]),
        "bot_login": str(review_state["bot_login"]),
        "is_draft": bool(initial["isDraft"]),
        "force_full_review": force_full_review,
        "last_review_sha": last_review_sha,
        "force_pushed_since": force_pushed,
        "incremental_path": incremental_path,
    }
    if not _emit_json(context):
        return 1
    Path(os.environ.get("REVIEWED_HEAD_FILE", "/tmp/reviewed-head")).write_text(
        f"{head_sha}\n"
    )
    return 0


def _parse_args(args: list[str]) -> tuple[str, str | None, list[str]] | None:
    if not args or not args[0].isdigit():
        return None
    pr = args[0]
    rest = args[1:]
    edit_review: str | None = None
    if rest[:1] == ["--edit-review"]:
        if len(rest) < 2 or not rest[1].isdigit():
            _error("--edit-review needs a numeric review id")
            return None
        edit_review = rest[1]
        rest = rest[2:]
    if rest[:1] == ["--"]:
        command = rest[1:]
        if not command:
            _error("-- needs a command")
            return None
    elif rest:
        _error(f"unexpected arguments: {' '.join(rest)}")
        return None
    else:
        command = []
    return pr, edit_review, command


def _post(args: list[str]) -> int:
    parsed = _parse_args(args)
    if parsed is None:
        if not args or not args[0].isdigit():
            print(
                f"usage: {sys.argv[0]} post <pr-number> "
                "[--edit-review <id>] [-- command ...]",
                file=sys.stderr,
            )
        return 1
    pr, edit_review, command = parsed

    repo = github_cli.repository()
    pin_file = Path(os.environ.get("REVIEWED_HEAD_FILE", "/tmp/reviewed-head"))
    try:
        reviewed = pin_file.read_text().strip()
    except OSError:
        reviewed = ""
    if not SHA_RE.fullmatch(reviewed):
        return _error(f"{pin_file} does not hold a commit sha")

    initial = _pr_view(pr, repo, "headRefOid,state,baseRefOid")
    if not initial or not all(
        initial.get(key) for key in ("headRefOid", "state", "baseRefOid")
    ):
        return _error(f"could not read PR #{pr}")
    current_head = str(initial["headRefOid"])
    state = str(initial["state"])
    base_sha = str(initial["baseRefOid"])
    if state != "OPEN":
        return _skip(f"PR is {state}")

    retargeted = current_head != reviewed
    delta_file: str | None = None
    if retargeted:
        if command:
            return _skip(f"HEAD moved to {current_head} before the outward action")
        subprocess.run(
            ["git", "fetch", "--no-tags", "--quiet", "origin", f"refs/pull/{pr}/head"],
            check=False,
        )
        subprocess.run(
            ["git", "fetch", "--no-tags", "--quiet", "origin", base_sha],
            check=False,
        )
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", reviewed, current_head],
            check=False,
        )
        if ancestor.returncode == 1:
            return _skip(
                f"cannot re-target onto {current_head} — {reviewed} is no longer "
                "an ancestor; leaving it to the queued review"
            )
        if ancestor.returncode:
            print(
                f"review-preflight: git merge-base failed with status "
                f"{ancestor.returncode}",
                file=sys.stderr,
            )
            return ancestor.returncode
        status, delta_file = _write_delta(reviewed, current_head, base_sha)
        if status:
            return status
        reviewed = current_head

    review_state = bot_review_state.fetch_review_state(pr, repo=repo)
    if review_state.get("head_sha") != reviewed:
        return _skip(
            "HEAD moved to "
            f"{review_state.get('head_sha') or 'an unreadable value'} during the preflight"
        )

    at_head = review_state.get("at_head")
    if edit_review is not None:
        editable = (
            isinstance(at_head, dict)
            and str(at_head.get("id")) == edit_review
            and str(review_state.get("orphan_id")) == edit_review
        )
        already = (
            "" if editable else f"cannot edit requested orphan review {edit_review}"
        )
    elif at_head is None or (
        _event_forces_review()
        and isinstance(at_head, dict)
        and at_head.get("draft_mode") is True
    ):
        already = ""
    else:
        already = f"already carries a {at_head['state']} review {at_head['id']}"
    if already:
        return _skip(f"{reviewed} {already}")

    final = _pr_view(pr, repo, "headRefOid,state")
    if not final or not all(final.get(key) for key in ("headRefOid", "state")):
        return _error(f"could not re-read PR #{pr}")
    final_head = str(final["headRefOid"])
    final_state = str(final["state"])
    if final_state != "OPEN":
        return _skip(f"PR is {final_state}")
    if final_head != reviewed:
        return _skip(f"HEAD moved to {final_head} during the preflight")

    if retargeted:
        output = (
            f"post: re-targeted onto {reviewed} — read the delta before posting\n"
            f"delta: {delta_file}\n"
        )
    else:
        output = f"post: {reviewed} is still the head you reviewed\n"
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except OSError:
        return 1
    if retargeted:
        pin_file.write_text(f"{reviewed}\n")
    if command:
        return subprocess.run(command, check=False).returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "start" and args[1].isdigit():
        return _start(args[1])
    if args[:1] == ["post"]:
        return _post(args[1:])
    print(
        f"usage: {sys.argv[0]} start <pr-number> | "
        "post <pr-number> [--edit-review <id>] [-- command ...]",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
