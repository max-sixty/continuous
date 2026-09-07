# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Bind review sessions and publication to canonical state for the reviewed PR.

``start`` records the consistent snapshot a session reviews. ``post`` reports
whether a narrative review may proceed, and requires a second call to accept a
queued delta after the agent reads it. ``submit`` is the final boundary for
GitHub review API writes and never retargets onto a head the session did not
inspect. ``complete`` durably acknowledges a ready-for-review generation when
a completed pass deliberately publishes no reader-facing review.

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


def _compatible_pending_review(
    review: dict[str, Any],
    *,
    is_draft: bool,
    ready_review_event_id: int | None,
) -> bool:
    if is_draft:
        return review.get("draft_mode") is True
    if ready_review_event_id is not None:
        return ready_review_event_id in review.get("ready_review_ids", [])
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
    if not review_state.get("needs_review"):
        return _skip(f"PR #{pr} has no outstanding review demand")

    outstanding = review_state.get("outstanding_ready_for_review")
    ready_review_event_id = (
        int(outstanding["id"])
        if not bool(initial["isDraft"]) and isinstance(outstanding, dict)
        else None
    )
    force_full_review = not bool(initial["isDraft"]) and (
        ready_review_event_id is not None
    )
    compatible_pending = [
        review
        for review in review_state.get("pending_reviews", [])
        if review.get("sha") == head_sha
        if _compatible_pending_review(
            review,
            is_draft=bool(initial["isDraft"]),
            ready_review_event_id=ready_review_event_id,
        )
    ]
    recovery_pending_review_id = (
        int(compatible_pending[-1]["id"]) if compatible_pending else None
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
        "recovery_pending_review_id": recovery_pending_review_id,
        "operation_id": uuid.uuid4().hex,
    }
    if not _emit_json(context):
        return 1
    _write_private_json(
        context_path,
        {
            "operation_id": context["operation_id"],
            "review_mode": "draft" if bool(initial["isDraft"]) else "full",
            "full_non_draft": force_full_review,
            "ready_review_event_id": ready_review_event_id,
            "recovery_pending_review_id": recovery_pending_review_id,
            "pending_review_ids": [
                int(review["id"]) for review in review_state.get("pending_reviews", [])
            ],
            "retarget_candidate_head": None,
        },
    )
    pin_path.write_text(f"{head_sha}\n")
    return 0


def _captured_readiness_is_outstanding(review_state: dict[str, Any]) -> bool:
    context = _read_context()
    if context is None or context.get("full_non_draft") is not True:
        return False
    event_id = context.get("ready_review_event_id")
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
    pr: str, *, allow_retarget: bool, allow_covered: bool = False
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
    readiness_outstanding = _captured_readiness_is_outstanding(review_state)
    if at_head is None or readiness_outstanding:
        already = ""
    else:
        already = f"already carries a {at_head['state']} review {at_head['id']}"
    if already and not allow_covered:
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
    status, snapshot = _publication_snapshot(args[0], allow_retarget=True)
    if snapshot is None:
        return status

    reviewed = str(snapshot["reviewed"])
    context = _read_context()
    if context is None:
        return _error("review context is missing; run start again")
    accepts_candidate = bool(
        snapshot["retargeted"] and context.get("retarget_candidate_head") == reviewed
    )
    if snapshot["retargeted"] and not accepts_candidate:
        output = (
            f"post: candidate head {reviewed} — read the delta, then run post again\n"
            f"delta: {snapshot['delta_file']}\n"
        )
        records_candidate = True
    elif snapshot["retargeted"]:
        output = f"post: accepted reviewed delta through {reviewed}\n"
        records_candidate = False
    else:
        output = f"post: {reviewed} is still the head you reviewed\n"
        records_candidate = False
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except OSError:
        return 1
    if records_candidate:
        context["retarget_candidate_head"] = reviewed
        _write_private_json(_context_path(), context)
    if accepts_candidate:
        _pin_path().write_text(f"{reviewed}\n")
        context["retarget_candidate_head"] = None
        _write_private_json(_context_path(), context)
    return 0


def _read_body(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError as error:
        _error(f"could not read review body {path}: {error}")
        return None


def _body_with_markers(body: str, *, readiness_marker: str | None) -> str:
    public = bot_review_state.strip_review_metadata(body).rstrip()
    markers = []
    if readiness_marker is not None:
        markers.append(readiness_marker)
    return "\n\n".join([part for part in (public, *markers) if part])


def _readiness_marker(snapshot: dict[str, Any]) -> str | None:
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
    quiet: bool = False,
) -> dict[str, Any]:
    endpoint = f"repos/{repo}/pulls/{pr}/reviews"
    result = github_cli.json_call(
        "api",
        endpoint,
        "--method",
        "POST",
        "--input",
        "-",
        input=json.dumps(payload),
        quiet=quiet,
    )
    if not isinstance(result, dict):
        raise TypeError("review API returned a non-object")
    return result


def _review_id(result: dict[str, Any]) -> int:
    if not isinstance(result, dict):
        raise TypeError("review API returned a non-object")
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
        if option not in {"--event", "--body-file", "--payload-file"}:
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


def _discard_pending_reviews(snapshot: dict[str, Any]) -> int:
    context = _read_context() or {}
    captured = context.get("pending_review_ids")
    if not isinstance(captured, list):
        return 0
    live = {
        review.get("id")
        for review in snapshot["review_state"].get("pending_reviews", [])
    }
    for review_id in captured:
        if not isinstance(review_id, int) or review_id <= 0 or review_id not in live:
            continue
        try:
            github_cli.run(
                "api",
                f"repos/{snapshot['repo']}/pulls/{snapshot['pr']}/reviews/{review_id}",
                "--method",
                "DELETE",
            )
        except subprocess.CalledProcessError as error:
            return _report_api_error(error)
    return 0


def _matching_pending(pr: str, repo: str, *, operation_id: str) -> int | None:
    state = bot_review_state.fetch_review_state(pr, repo=repo)
    matches = [
        int(review["id"])
        for review in state.get("pending_reviews", [])
        if review.get("operation_id") == operation_id
    ]
    return matches[-1] if matches else None


def _complete(args: list[str]) -> int:
    if len(args) != 1 or not args[0].isdigit():
        print(f"usage: {sys.argv[0]} complete <pr-number>", file=sys.stderr)
        return 1
    pr = args[0]
    status, snapshot = _publication_snapshot(
        pr, allow_retarget=False, allow_covered=True
    )
    if snapshot is None:
        return status
    snapshot["pr"] = pr
    discard_status = _discard_pending_reviews(snapshot)
    if discard_status:
        return discard_status
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
            "--event COMMENT (--body-file <path> | --payload-file <path>))",
            file=sys.stderr,
        )
        return 1
    pr, options = parsed
    event = options.get("--event")
    body_path = options.get("--body-file")
    payload_path = options.get("--payload-file")
    if event not in {"APPROVE", "COMMENT"}:
        return _error("--event must be APPROVE or COMMENT")
    elif payload_path is not None and (event != "COMMENT" or body_path is not None):
        return _error("--payload-file is exclusive to COMMENT")
    elif event == "COMMENT" and body_path is None and payload_path is None:
        return _error("COMMENT requires --body-file or --payload-file")

    status, snapshot = _publication_snapshot(pr, allow_retarget=False)
    if snapshot is None:
        return status
    context = _read_context()
    if event == "APPROVE" and (context or {}).get("review_mode") == "draft":
        return _skip("review started while PR was a draft; approval requires a new run")
    if event == "APPROVE" and snapshot["is_draft"]:
        return _skip("PR became a draft before approval")
    snapshot["pr"] = pr

    marker = _readiness_marker(snapshot)
    repo = str(snapshot["repo"])
    reviewed = str(snapshot["reviewed"])

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
    )
    if comments and not final_body:
        final_body = "See the inline findings."
    endpoint_payload: dict[str, Any] = {
        "event": event,
        "commit_id": reviewed,
        "body": final_body,
    }
    discard_status = _discard_pending_reviews(snapshot)
    if discard_status:
        return discard_status
    if not comments:
        try:
            posted = _review_id(_review_api(repo, pr, payload=endpoint_payload))
        except subprocess.CalledProcessError as error:
            return _report_api_error(error)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return _error(str(error))
        print(f"posted: review {posted}")
        return 0

    operation_id = str((context or {}).get("operation_id") or "")
    try:
        review_mode = str((context or {}).get("review_mode") or "")
        operation_marker = bot_review_state.review_operation_marker(
            operation_id, review_mode
        )
    except ValueError as error:
        return _error(str(error))
    endpoint_payload.pop("event")
    endpoint_payload["body"] = "\n\n".join(
        part for part in (final_body, operation_marker) if part
    )
    endpoint_payload["comments"] = comments
    try:
        posted = _review_id(_review_api(repo, pr, payload=endpoint_payload, quiet=True))
    except subprocess.CalledProcessError as error:
        try:
            pending = _matching_pending(pr, repo, operation_id=operation_id)
        except (ValueError, TypeError, subprocess.CalledProcessError):
            pending = None
        if pending is not None:
            print(f"recover: pending review {pending}")
        else:
            print("uncertain: review submission outcome unknown")
        return _report_api_error(error)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return _error(str(error))

    try:
        finalized = _review_id(
            github_cli.json_call(
                "api",
                f"repos/{repo}/pulls/{pr}/reviews/{posted}/events",
                "--method",
                "POST",
                "--input",
                "-",
                input=json.dumps({"event": "COMMENT", "body": final_body}),
            )
        )
    except subprocess.CalledProcessError as error:
        print(f"recover: pending review {posted}")
        return _report_api_error(error)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"recover: pending review {posted}")
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
