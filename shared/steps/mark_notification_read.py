"""Mark the notification thread for the triggering event read.

The scheduled ``tend-notifications`` poll otherwise burns tokens rediscovering
work this run already did. Shared verbatim by both harness actions, which gate
the step on a successful run (``if: success()``).

Decisions this encodes:

- A thread is marked only when its ``updated_at`` predates this run's start.
  Activity that arrived mid-run is what the next workflow run has to see, so
  it stays unread.
- Without ``run_started_at`` that comparison cannot be made, and marking
  unconditionally would swallow exactly the mid-run activity the guard exists
  to preserve — so a failed or absent timestamp skips this cycle and leaves
  the thread to the scheduled poll.
- The agent run already succeeded, so nothing here may fail the step: a
  transient API error warns and returns 0.
- ``issue_comment`` fires for both issues and PR conversation comments, but a
  PR notification's ``subject.url`` always names ``/pulls/N``; the issue's
  ``pull_request`` field is what tells the two apart.

Inputs (env): ``GITHUB_EVENT_NAME``, ``GITHUB_EVENT_PATH``,
``GITHUB_REPOSITORY``, ``GITHUB_RUN_ID`` (from Actions), plus the bot's
``GITHUB_TOKEN``, which reaches ``gh`` through the environment.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import _common


def subject_url(repo: str, event_name: str, event: Any) -> str | None:
    """The notification ``subject.url`` for the triggering event.

    ``None`` for an event that names no single issue or PR, which is nothing
    to mark read — and equally for one that should name a number but does not,
    since this step runs only after the agent already succeeded and a payload
    shape it cannot read must not turn that green run red.
    """
    if not isinstance(event, dict):
        return None
    if event_name in (
        "pull_request_target",
        "pull_request_review",
        "pull_request_review_comment",
    ):
        section, kind = event.get("pull_request"), "pulls"
    elif event_name in ("issue_comment", "issues"):
        section = event.get("issue")
        on_a_pr = isinstance(section, dict) and section.get("pull_request")
        kind = "pulls" if event_name == "issue_comment" and on_a_pr else "issues"
    else:
        return None
    number = section.get("number") if isinstance(section, dict) else None
    if number is None:
        return None
    return f"https://api.github.com/repos/{repo}/{kind}/{number}"


def _predates(notification: Any, url: str, run_started_at: str) -> bool:
    """Whether *notification* is a thread for *url* last touched before the run."""
    if not isinstance(notification, dict):
        return False
    subject = notification.get("subject")
    if not isinstance(subject, dict) or subject.get("url") != url:
        return False
    updated_at = notification.get("updated_at")
    return isinstance(updated_at, str) and updated_at <= run_started_at


def threads_to_mark(
    notifications: list[Any], url: str, run_started_at: str
) -> list[str]:
    """The ids of the threads for *url* whose activity predates the run.

    Both timestamps are ISO-8601 in UTC, so comparing them as strings orders
    them chronologically. A notification carrying no stamp is left unread for
    the same reason a run with no ``run_started_at`` marks nothing: its age is
    unknown, and the mid-run activity the guard exists to preserve is exactly
    what marking it anyway would swallow.
    """
    return [
        str(notification["id"])
        for notification in notifications
        if _predates(notification, url, run_started_at)
        and notification.get("id") is not None
    ]


def main() -> int:
    env = _common.require_env(
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
    )
    repo = env["GITHUB_REPOSITORY"]

    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    url = subject_url(repo, env["GITHUB_EVENT_NAME"], event)
    if url is None:
        return 0

    run_id = env["GITHUB_RUN_ID"]
    try:
        run = _common.gh_json("api", f"repos/{repo}/actions/runs/{run_id}")
    except _common.GH_READ_FAILED:
        run = None
    run_started_at = run.get("run_started_at") if isinstance(run, dict) else None
    if not isinstance(run_started_at, str) or not run_started_at:
        _common.annotate(
            "warning",
            "Could not read run_started_at; leaving notification unread (non-fatal)",
        )
        return 0

    # An unreachable API, or a 200 carrying HTML or an error object rather than
    # the inbox, is the same non-fatal outcome: warn and leave the thread.
    try:
        notifications = _common.gh_json("api", "notifications")
    except _common.GH_READ_FAILED:
        notifications = None
    if not isinstance(notifications, list):
        _common.annotate("warning", "Failed to mark notification as read (non-fatal)")
        return 0

    for thread_id in threads_to_mark(notifications, url, run_started_at):
        try:
            _common.gh("api", f"notifications/threads/{thread_id}", "-X", "PATCH")
        except subprocess.CalledProcessError:
            continue
    return 0


if __name__ == "__main__":
    _common.run(main)
