# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Bind review sessions and publication to canonical state for the reviewed PR.

``start`` records the consistent snapshot a session reviews. ``post`` reports
whether a narrative review may proceed, including a compatible queued delta;
``submit`` is the final boundary for GitHub review API writes and never
retargets onto a head the session did not inspect. ``complete`` durably
acknowledges a ready-for-review generation when a pass deliberately publishes
no reader-facing review.

Keeping snapshotting, re-targeting, completion, and submission in one review-
domain command makes their shared state explicit without turning unrelated
runner helpers into a single application.

Run this script directly with ``/usr/bin/python3 -E -s``, not through ``uv``.
The reviewed-head pin advances only after the status reaches stdout; ``uv run``
reopens a closed stdout for its child and would hide a failed delivery. The
workflow's system interpreter is the same isolated standard-library runtime used
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
import uuid
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


def _context_path() -> Path:
    return Path(os.environ.get("REVIEW_CONTEXT_FILE", "/tmp/tend-review-context.json"))


def _pin_path() -> Path:
    return Path(os.environ.get("REVIEWED_HEAD_FILE", "/tmp/reviewed-head"))


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_context() -> dict[str, Any] | None:
    try:
        value = json.loads(_context_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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


def _compatible_incomplete_review(
    review: dict[str, Any],
    *,
    is_draft: bool,
    ready_review_event_id: int | None,
    event_forces_review: bool,
) -> bool:
    if is_draft:
        return review.get("draft_mode") is True
    if ready_review_event_id is not None:
        return ready_review_event_id in review.get("ready_review_ids", [])
    if event_forces_review:
        return False
    return review.get("draft_mode") is False and not review.get("ready_review_ids")


def _start(pr: str) -> int:
    context_path = _context_path()
    pin_path = _pin_path()
    context_path.unlink(missing_ok=True)
    pin_path.unlink(missing_ok=True)

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

    outstanding = review_state.get("outstanding_ready_for_review")
    ready_review_event_id = (
        int(outstanding["id"])
        if not bool(initial["isDraft"]) and isinstance(outstanding, dict)
        else None
    )
    event_forces_review = _event_forces_review()
    force_full_review = not bool(initial["isDraft"]) and (
        event_forces_review or ready_review_event_id is not None
    )
    compatible_incomplete = [
        review
        for review in review_state.get("incomplete_reviews", [])
        if _compatible_incomplete_review(
            review,
            is_draft=bool(initial["isDraft"]),
            ready_review_event_id=ready_review_event_id,
            event_forces_review=event_forces_review,
        )
    ]
    recovery_review_id = (
        int(compatible_incomplete[-1]["id"]) if compatible_incomplete else None
    )
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
        "ready_review_event_id": ready_review_event_id,
        "recovery_review_id": recovery_review_id,
        "operation_id": uuid.uuid4().hex,
    }
    if not _emit_json(context):
        return 1
    _write_private_json(
        context_path,
        {
            "operation_id": context["operation_id"],
            "full_non_draft": force_full_review,
            "ready_review_event_id": ready_review_event_id,
            "recovery_review_id": recovery_review_id,
        },
    )
    pin_path.write_text(f"{head_sha}\n")
    return 0


def _captured_readiness_is_outstanding(
    review_state: dict[str, Any], *, is_draft: bool
) -> bool:
    if is_draft:
        return False
    context = _read_context()
    if context is None:
        return _event_forces_review()
    if context.get("full_non_draft") is not True:
        return False
    event_id = context.get("ready_review_event_id")
    if event_id is None:
        return _event_forces_review()
    if not isinstance(event_id, int) or event_id <= 0:
        return False
    acknowledged = review_state.get("acknowledged_ready_ids", [])
    if event_id in acknowledged:
        return False
    latest = review_state.get("latest_ready_for_review")
    outstanding = review_state.get("outstanding_ready_for_review")
    return not (
        isinstance(latest, dict)
        and latest.get("id") != event_id
        and outstanding is None
    )


def _publication_snapshot(
    pr: str, *, edit_review: int | None, allow_retarget: bool
) -> tuple[int, dict[str, Any] | None]:
    repo = github_cli.repository()
    pin_file = _pin_path()
    try:
        reviewed = pin_file.read_text().strip()
    except OSError:
        reviewed = ""
    if not SHA_RE.fullmatch(reviewed):
        return _error(f"{pin_file} does not hold a commit sha"), None

    initial = _pr_view(pr, repo, "headRefOid,state,baseRefOid,isDraft")
    if not initial or not all(
        initial.get(key) is not None
        for key in ("headRefOid", "state", "baseRefOid", "isDraft")
    ):
        return _error(f"could not read PR #{pr}"), None
    current_head = str(initial["headRefOid"])
    state = str(initial["state"])
    base_sha = str(initial["baseRefOid"])
    if state != "OPEN":
        return _skip(f"PR is {state}"), None

    retargeted = current_head != reviewed
    delta_file: str | None = None
    if retargeted:
        if not allow_retarget:
            return (
                _skip(f"HEAD moved to {current_head} before the outward action"),
                None,
            )
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
            return (
                _skip(
                    f"cannot re-target onto {current_head} — {reviewed} is no longer "
                    "an ancestor; leaving it to the queued review"
                ),
                None,
            )
        if ancestor.returncode:
            print(
                f"review-preflight: git merge-base failed with status "
                f"{ancestor.returncode}",
                file=sys.stderr,
            )
            return ancestor.returncode, None
        status, delta_file = _write_delta(reviewed, current_head, base_sha)
        if status:
            return status, None
        reviewed = current_head

    review_state = bot_review_state.fetch_review_state(pr, repo=repo)
    if review_state.get("head_sha") != reviewed:
        return (
            _skip(
                "HEAD moved to "
                f"{review_state.get('head_sha') or 'an unreadable value'} during the preflight"
            ),
            None,
        )

    at_head = review_state.get("at_head")
    readiness_outstanding = _captured_readiness_is_outstanding(
        review_state, is_draft=bool(initial["isDraft"])
    )
    if edit_review is not None:
        context = _read_context() or {}
        editable = any(
            item.get("id") == edit_review
            and item.get("sha") == reviewed
            and (
                item.get("operation_id") == context.get("operation_id")
                or edit_review == context.get("recovery_review_id")
            )
            for item in review_state.get("incomplete_reviews", [])
        ) and (at_head is None or readiness_outstanding)
        already = "" if editable else f"cannot edit incomplete review {edit_review}"
    elif at_head is None or readiness_outstanding:
        already = ""
    else:
        already = f"already carries a {at_head['state']} review {at_head['id']}"
    if already:
        return _skip(f"{reviewed} {already}"), None

    final = _pr_view(pr, repo, "headRefOid,state,isDraft")
    if not final or not all(
        final.get(key) is not None for key in ("headRefOid", "state", "isDraft")
    ):
        return _error(f"could not re-read PR #{pr}"), None
    final_head = str(final["headRefOid"])
    final_state = str(final["state"])
    if final_state != "OPEN":
        return _skip(f"PR is {final_state}"), None
    if final_head != reviewed:
        return _skip(f"HEAD moved to {final_head} during the preflight"), None

    return 0, {
        "repo": repo,
        "reviewed": reviewed,
        "review_state": review_state,
        "is_draft": bool(final["isDraft"]),
        "retargeted": retargeted,
        "delta_file": delta_file,
    }


def _post(args: list[str]) -> int:
    if len(args) != 1 or not args[0].isdigit():
        print(f"usage: {sys.argv[0]} post <pr-number>", file=sys.stderr)
        return 1
    status, snapshot = _publication_snapshot(
        args[0], edit_review=None, allow_retarget=True
    )
    if snapshot is None:
        return status

    reviewed = str(snapshot["reviewed"])
    if snapshot["retargeted"]:
        output = (
            f"post: re-targeted onto {reviewed} — read the delta before posting\n"
            f"delta: {snapshot['delta_file']}\n"
        )
    else:
        output = f"post: {reviewed} is still the head you reviewed\n"
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except OSError:
        return 1
    if snapshot["retargeted"]:
        _pin_path().write_text(f"{reviewed}\n")
    return 0


def _read_body(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError as error:
        _error(f"could not read review body {path}: {error}")
        return None


def _body_with_markers(
    body: str, *, readiness_marker: str | None, is_draft: bool
) -> str:
    public = bot_review_state.strip_review_metadata(body).rstrip()
    markers = []
    if is_draft and bot_review_state.DRAFT_REVIEW_MARKER not in public:
        markers.append(bot_review_state.DRAFT_REVIEW_MARKER)
    if readiness_marker is not None:
        markers.append(readiness_marker)
    return "\n\n".join([part for part in (public, *markers) if part])


def _readiness_marker(snapshot: dict[str, Any]) -> str | None:
    if snapshot["is_draft"]:
        return None
    context = _read_context()
    if context is None or context.get("full_non_draft") is not True:
        return None
    event_id = context.get("ready_review_event_id")
    if not isinstance(event_id, int) or event_id <= 0:
        return None
    if event_id in snapshot["review_state"].get("acknowledged_ready_ids", []):
        return None
    return bot_review_state.ready_review_marker(event_id)


def _review_api(
    repo: str,
    pr: str,
    *,
    payload: dict[str, Any],
    review_id: int | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    endpoint = f"repos/{repo}/pulls/{pr}/reviews"
    method = "POST"
    if review_id is not None:
        endpoint = f"{endpoint}/{review_id}"
        method = "PUT"
    result = github_cli.json_call(
        "api",
        endpoint,
        "--method",
        method,
        "--input",
        "-",
        input=json.dumps(payload),
        quiet=quiet,
    )
    if not isinstance(result, dict):
        raise TypeError("review API returned a non-object")
    return result


def _review_id(result: dict[str, Any]) -> int:
    review_id = result.get("id")
    if not isinstance(review_id, int) or review_id <= 0:
        raise ValueError("review API response has no numeric id")
    return review_id


def _parse_submit(args: list[str]) -> tuple[str, dict[str, str]] | None:
    if not args or not args[0].isdigit():
        return None
    options: dict[str, str] = {}
    rest = args[1:]
    while rest:
        option = rest.pop(0)
        if option not in {"--event", "--body-file", "--payload-file", "--edit-review"}:
            _error(f"unexpected argument: {option}")
            return None
        if option in options or not rest:
            _error(f"{option} needs one value and may be passed once")
            return None
        options[option] = rest.pop(0)
    return args[0], options


def _report_api_error(error: subprocess.CalledProcessError) -> int:
    if error.stderr:
        sys.stderr.write(error.stderr)
    return github_cli.exit_code(error)


def _matching_incomplete(pr: str, repo: str, *, operation_id: str) -> int | None:
    state = bot_review_state.fetch_review_state(pr, repo=repo)
    matches = [
        int(review["id"])
        for review in state.get("incomplete_reviews", [])
        if review.get("operation_id") == operation_id
    ]
    return matches[-1] if matches else None


def _complete(args: list[str]) -> int:
    if len(args) != 1 or not args[0].isdigit():
        print(f"usage: {sys.argv[0]} complete <pr-number>", file=sys.stderr)
        return 1
    pr = args[0]
    status, snapshot = _publication_snapshot(pr, edit_review=None, allow_retarget=False)
    if snapshot is None:
        return status
    marker = _readiness_marker(snapshot)
    if marker is None:
        return _skip("no captured ready-for-review generation needs acknowledgment")
    try:
        posted = _review_id(
            _review_api(
                str(snapshot["repo"]),
                pr,
                payload={
                    "event": "COMMENT",
                    "commit_id": str(snapshot["reviewed"]),
                    "body": marker,
                },
            )
        )
    except subprocess.CalledProcessError as error:
        return _report_api_error(error)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return _error(str(error))
    print(f"acknowledged: review {posted}")
    return 0


def _submit(args: list[str]) -> int:
    parsed = _parse_submit(args)
    if parsed is None:
        print(
            f"usage: {sys.argv[0]} submit <pr-number> "
            "(--event APPROVE [--body-file <path>] | "
            "--event COMMENT (--body-file <path> | --payload-file <path>) | "
            "--edit-review <id> --body-file <path>)",
            file=sys.stderr,
        )
        return 1
    pr, options = parsed
    edit_text = options.get("--edit-review")
    edit_review = int(edit_text) if edit_text and edit_text.isdigit() else None
    if edit_text is not None and edit_review is None:
        return _error("--edit-review needs a numeric review id")

    event = options.get("--event")
    body_path = options.get("--body-file")
    payload_path = options.get("--payload-file")
    if edit_review is not None:
        if event is not None or payload_path is not None or body_path is None:
            return _error("--edit-review requires only --body-file")
    elif event not in {"APPROVE", "COMMENT"}:
        return _error("--event must be APPROVE or COMMENT")
    elif payload_path is not None and (event != "COMMENT" or body_path is not None):
        return _error("--payload-file is exclusive to COMMENT")
    elif event == "COMMENT" and body_path is None and payload_path is None:
        return _error("COMMENT requires --body-file or --payload-file")

    status, snapshot = _publication_snapshot(
        pr, edit_review=edit_review, allow_retarget=False
    )
    if snapshot is None:
        return status
    if event == "APPROVE" and snapshot["is_draft"]:
        return _skip("PR became a draft before approval")

    marker = _readiness_marker(snapshot)
    repo = str(snapshot["repo"])
    reviewed = str(snapshot["reviewed"])

    if edit_review is not None:
        body = _read_body(str(body_path))
        if body is None:
            return 1
        try:
            result = _review_api(
                repo,
                pr,
                review_id=edit_review,
                payload={
                    "body": _body_with_markers(
                        body,
                        readiness_marker=marker,
                        is_draft=bool(snapshot["is_draft"]),
                    )
                },
            )
            posted = _review_id(result)
        except subprocess.CalledProcessError as error:
            return _report_api_error(error)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return _error(str(error))
        print(f"posted: review {posted}")
        return 0

    public_body = ""
    comments: list[dict[str, Any]] = []
    if body_path is not None:
        body = _read_body(body_path)
        if body is None:
            return 1
        public_body = body
    elif payload_path is not None:
        try:
            payload = json.loads(Path(payload_path).read_text())
        except (OSError, json.JSONDecodeError) as error:
            return _error(f"could not read review payload {payload_path}: {error}")
        if not isinstance(payload, dict) or not isinstance(
            payload.get("body", ""), str
        ):
            return _error("review payload must be an object with a string body")
        raw_comments = payload.get("comments")
        if (
            not isinstance(raw_comments, list)
            or not raw_comments
            or not all(isinstance(comment, dict) for comment in raw_comments)
        ):
            return _error("review payload must contain a non-empty comments list")
        public_body = str(payload.get("body", ""))
        comments = raw_comments

    final_body = _body_with_markers(
        public_body,
        readiness_marker=marker,
        is_draft=bool(snapshot["is_draft"]),
    )
    endpoint_payload: dict[str, Any] = {
        "event": event,
        "commit_id": reviewed,
        "body": final_body,
    }
    if not comments:
        try:
            posted = _review_id(_review_api(repo, pr, payload=endpoint_payload))
        except subprocess.CalledProcessError as error:
            return _report_api_error(error)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return _error(str(error))
        print(f"posted: review {posted}")
        return 0

    context = _read_context()
    operation_id = str((context or {}).get("operation_id") or "")
    try:
        incomplete_marker = bot_review_state.incomplete_review_marker(operation_id)
    except ValueError as error:
        return _error(str(error))
    endpoint_payload["body"] = "\n\n".join(
        [
            incomplete_marker,
            _body_with_markers(
                "",
                readiness_marker=marker,
                is_draft=bool(snapshot["is_draft"]),
            ),
        ]
    ).rstrip()
    endpoint_payload["comments"] = comments
    try:
        posted = _review_id(_review_api(repo, pr, payload=endpoint_payload, quiet=True))
    except subprocess.CalledProcessError as error:
        try:
            incomplete = _matching_incomplete(pr, repo, operation_id=operation_id)
        except (ValueError, TypeError, subprocess.CalledProcessError):
            incomplete = None
        if incomplete is not None:
            print(f"recover: incomplete review {incomplete}")
        else:
            print("uncertain: review submission outcome unknown")
        return _report_api_error(error)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return _error(str(error))

    try:
        finalized = _review_id(
            _review_api(
                repo,
                pr,
                review_id=posted,
                payload={"body": final_body},
            )
        )
    except subprocess.CalledProcessError as error:
        print(f"recover: incomplete review {posted}")
        return _report_api_error(error)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"recover: incomplete review {posted}")
        return _error(str(error))
    print(f"posted: review {finalized}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "start" and args[1].isdigit():
        return _start(args[1])
    if args[:1] == ["complete"]:
        return _complete(args[1:])
    if args[:1] == ["post"]:
        return _post(args[1:])
    if args[:1] == ["submit"]:
        return _submit(args[1:])
    print(
        f"usage: {sys.argv[0]} start|post|submit|complete <pr-number> ...",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(github_cli.exit_code(error)) from None
