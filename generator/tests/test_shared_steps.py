"""Tests for the step bodies that are still shell scripts.

Two homes: the composite actions' remaining shell steps under shared/steps/
(the fork-PR instruction pinning, run as `bash <script>` inside both harness
actions) and the generator's template scripts (generator/src/tend/templates/
*.sh), which are inlined into generated workflows. In both, a non-zero exit
fails the step, and shellcheck (pre-commit) can't catch runtime behaviour, so
each is driven here through a fake `gh` on PATH. The Python step bodies test
themselves beside their modules in shared/steps/test_*.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

REPO_ROOT = Path(__file__).resolve().parents[2]
PIN_INSTRUCTION_FILES = REPO_ROOT / "shared" / "steps" / "pin-instruction-files.sh"
RESTORE_SENSITIVE_CONFIG = (
    REPO_ROOT / "shared" / "steps" / "restore-sensitive-config.sh"
)


# Fork-PR instruction pinning. One tampered checkout exercises every shape a
# fork can give an instruction path — a rewrite, a move, a directory's name
# pointed outside the checkout (so a write or delete through it would land
# there), a directory swapped for a file and a file for a directory, and files
# or a `.claude` symlink planted where the base has none. Both harnesses' pin
# scripts run against it and are held to the same end state.
_BASE = {
    "README.md": "base readme\n",
    "CLAUDE.md": "root guidance\n",
    "AGENTS.md": "-> CLAUDE.md",
    ".claude/skills/running-tend/SKILL.md": "root skill\n",
    "site/CLAUDE.md": "site guidance\n",
    "docs/CLAUDE.md": "docs guidance\n",
    "docs/CLAUDE.local.md": "docs local guidance\n",
    "nested/AGENTS.md": "nested guidance\n",
    "tools/CLAUDE.md": "tools guidance\n",
    "moved/CLAUDE.md": "moved guidance\n",
    "apps/web/.claude/skills/deploy/SKILL.md": "web skill\n",
}

# Targets a fork symlink could redirect a write or a delete to.
_OUTSIDE = {
    rel: "must not change\n"
    for rel in (".claude/skills/deploy/SKILL.md", "CLAUDE.md", "AGENTS.md")
}


def _write(path: Path, content: str) -> None:
    """Create *path* with *content*; `-> target` makes a symlink (the inverse
    of `_tree`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if content.startswith("-> "):
        path.unlink(missing_ok=True)
        path.symlink_to(content[3:])
    else:
        path.write_text(content)


def _tree(root: Path) -> dict[str, str]:
    """Every file under *root* by relative path: its text, or `-> target` for a
    symlink. Skips `.git/`."""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] == ".git":
            continue
        if path.is_symlink():
            tree[str(rel)] = f"-> {path.readlink()}"
        elif path.is_file():
            tree[str(rel)] = path.read_text()
    return tree


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _fork_pr(
    tmp_path: Path,
    base: dict[str, str],
    tamper: Callable[[Path], None],
    base_ref: str = "main",
) -> tuple[Path, Path]:
    """A fork PR's checkout as `actions/checkout` leaves it: a clone of an
    origin holding *base*, with *tamper*'s changes committed on top and
    `origin/main` at the base. The event names *base_ref* as the PR's base.
    Returns (repo, event)."""
    origin = tmp_path / "origin"
    repo = tmp_path / "repo"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    for rel, content in base.items():
        _write(origin / rel, content)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "base")
    _git(tmp_path, "clone", str(origin), str(repo))
    tamper(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fork tree")

    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "base": {"ref": base_ref},
                    "head": {"repo": {"fork": True}},
                },
                "repository": {"default_branch": base_ref},
            }
        )
    )
    return repo, event


def _tampered_checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (repo, outside, event) for the scenario above."""
    outside = tmp_path / "outside"
    for rel, content in _OUTSIDE.items():
        _write(outside / rel, content)

    def tamper(repo: Path) -> None:
        _write(repo / "README.md", "fork readme\n")
        _write(repo / "CLAUDE.md", "EVIL root\n")
        _write(repo / "AGENTS.md", f"-> {outside / 'AGENTS.md'}")
        _write(repo / ".claude/skills/running-tend/SKILL.md", "EVIL skill\n")
        _write(repo / ".claude/skills/fork-only/SKILL.md", "EVIL\n")
        _write(repo / ".claude/escape", f"-> {outside / 'CLAUDE.md'}")
        _write(repo / "CLAUDE.local.md", "EVIL\n")
        _write(repo / "site/CLAUDE.md", "EVIL site\n")
        shutil.rmtree(repo / "docs")
        _write(repo / "docs", f"-> {outside}")
        shutil.rmtree(repo / "nested")
        _write(repo / "nested", "a file now\n")
        (repo / "tools/CLAUDE.md").unlink()
        _write(repo / "tools/CLAUDE.md/child", "EVIL\n")
        # Same content at a new path: git reports this as a rename.
        _write(repo / "elsewhere/CLAUDE.md", _BASE["moved/CLAUDE.md"])
        (repo / "moved/CLAUDE.md").unlink()
        shutil.rmtree(repo / "apps/web")
        _write(repo / "apps/web", f"-> {outside}")
        _write(repo / "fork-only/CLAUDE.md", "EVIL\n")
        _write(repo / "fork-only/AGENTS.md", "EVIL\n")
        _write(repo / "notes/skills/deploy/SKILL.md", "EVIL\n")
        _write(repo / "site/.claude", "-> ../notes")
        _write(repo / "dir/CLAUDE.md/child", "not an instruction file\n")

    repo, event = _fork_pr(tmp_path, _BASE, tamper)
    return repo, outside, event


# The checkout after pinning: the base's instruction paths, the PR's own
# changes elsewhere, and fork content that isn't an instruction path — the
# directory a planted `.claude` symlink pointed at, and a directory named
# `CLAUDE.md`, which neither CLI can read as an instruction file.
_PINNED = {
    **_BASE,
    "README.md": "fork readme\n",
    "notes/skills/deploy/SKILL.md": "EVIL\n",
    "dir/CLAUDE.md/child": "not an instruction file\n",
}


def _pin(script: Path, repo: Path, event: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(script)],
        cwd=repo,
        env={
            "PATH": tool_path(),
            "GITHUB_EVENT_NAME": "pull_request_target",
            "GITHUB_EVENT_PATH": str(event),
        },
        capture_output=True,
        text=True,
    )


def test_pin_instruction_files_matches_base_for_every_instruction_path(
    tmp_path: Path,
) -> None:
    """Codex harness on a fork PR: every instruction path ends at its base
    version, fork-added ones are gone, a fork symlink is replaced rather than
    written or deleted through, and the rest of the PR stays."""
    repo, outside, event = _tampered_checkout(tmp_path)

    result = _pin(PIN_INSTRUCTION_FILES, repo, event)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(repo) == _PINNED
    assert _tree(outside) == _OUTSIDE


def test_restore_sensitive_config_matches_base_for_every_instruction_path(
    tmp_path: Path,
) -> None:
    """Same contract under the Claude harness, plus its own: the revert stays
    out of the index so it can't ride into a commit the agent makes later. The
    end-state check also proves no copy of a fork file was left anywhere the
    agent can read, a copy made as the runner user having followed the fork's
    symlinks."""
    repo, outside, event = _tampered_checkout(tmp_path)

    result = _pin(RESTORE_SENSITIVE_CONFIG, repo, event)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(repo) == _PINNED
    assert _tree(outside) == _OUTSIDE
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged.stdout == "", staged.stdout


@pytest.mark.parametrize(
    "script", [PIN_INSTRUCTION_FILES, RESTORE_SENSITIVE_CONFIG], ids=["codex", "claude"]
)
def test_pinning_leaves_a_pr_that_touches_no_instruction_path_alone(
    tmp_path: Path, script: Path
) -> None:
    """The usual PR: nothing the pin covers changed, so there is nothing to
    restore and the step still exits cleanly."""
    repo, event = _fork_pr(
        tmp_path,
        {"README.md": "base readme\n"},
        lambda repo: _write(repo / "README.md", "fork readme\n"),
    )

    result = _pin(script, repo, event)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(repo) == {"README.md": "fork readme\n"}


@pytest.mark.parametrize(
    "script", [PIN_INSTRUCTION_FILES, RESTORE_SENSITIVE_CONFIG], ids=["codex", "claude"]
)
def test_pinning_fails_when_the_base_ref_is_missing(
    tmp_path: Path, script: Path
) -> None:
    """A base the checkout doesn't hold fails the step. The alternative, a diff
    against nothing that lists nothing, would let the agent start on the
    fork's instruction files with a log line saying 0 paths were pinned."""
    repo, event = _fork_pr(
        tmp_path,
        {"CLAUDE.md": "root guidance\n"},
        lambda repo: _write(repo / "CLAUDE.md", "EVIL root\n"),
        base_ref="missing",
    )

    result = _pin(script, repo, event)

    assert result.returncode != 0, result.stdout + result.stderr


def _calls(env: dict[str, str]) -> list[str]:
    return Path(env["GH_CALLS"]).read_text().splitlines()


def _comments(env: dict[str, str]) -> str:
    """Every comment body the fake `gh` was handed on stdin, concatenated."""
    return Path(env["COMMENT_BODIES"]).read_text()


def _output(env: dict[str, str], key: str) -> str:
    """The value a pre-check wrote to `$GITHUB_OUTPUT` under *key*.

    Exactly one write: a second would leave the step's consumers reading
    whichever line Actions kept, so two is a defect rather than a last-wins.
    """
    lines = Path(env["GITHUB_OUTPUT"]).read_text().splitlines()
    values = [line.split("=", 1)[1] for line in lines if line.startswith(f"{key}=")]
    assert len(values) == 1, f"expected exactly one {key}, got: {lines}"
    return values[0]


# The script is written for the Ubuntu runners' GNU date; macOS ships BSD
# date, which has no `-d`. Fixed values also make the day-scoping assertions
# deterministic: "today" is 2026-01-02.
# The relative offsets come first: every call also carries a format string, so
# matching that branch first would collapse them all onto one timestamp.
FAKE_DATE = r"""#!/usr/bin/env bash
case "$*" in
  *"30 minutes ago"*) echo "2026-01-02T11:30:00Z" ;;
  *"10 minutes ago"*) echo "2026-01-02T11:50:00Z" ;;
  *"%Y-%m-%dT%H:%M:%SZ"*) echo "2026-01-02T12:00:00Z" ;;
  *) echo "2026-01-02" ;;
esac
"""

# ---------------------------------------------------------------------------
# notifications-check.sh — the tend-notifications pre-check
# ---------------------------------------------------------------------------

NOTIFICATIONS_CHECK = (
    REPO_ROOT / "generator" / "src" / "tend" / "templates" / "notifications-check.sh"
)

# `gh` stand-in for the notifications pre-check. The real notifications call
# uses `--paginate --slurp`, so the fake puts each fixture item on its own page.
# Counting more than one item therefore exercises the page flattening too.
FAKE_GH_NOTIFICATIONS = (
    GH_PREAMBLE
    + r"""
case "$2" in
  notifications\?*)
    [ -z "${FAIL_NOTIFS:-}" ] || exit 1
    # A 200 carrying something other than JSON, verbatim.
    if [ -n "${RAW_BODY:-}" ]; then cat "$RAW_BODY"; exit 0; fi
    # GitHub applies the strict `before` boundary server-side. The fixture uses
    # the pre-check's fixed cutoff so boundary and fresh activity stay unread.
    jq -c --arg cutoff "$NOTIF_CUTOFF" \
      '[.[] | select(.updated_at < $cutoff)] | map([.])' "$NOTIFICATIONS_JSON"
    ;;
  repos/*/subscription)
    [ -z "${FAIL_SUBSCRIPTION_WRITE:-}" ] || exit 1
    echo "put" >> "$SUBSCRIPTION_WRITES"
    emit '{"subscribed":true,"ignored":false}'
    ;;
  *) exit 1 ;;
esac
"""
)

# The fake `date` puts "now" at 12:00, so the queue snapshot ends at 11:50.
NOTIF_FRESH = "2026-01-02T11:55:00Z"
NOTIF_SETTLED = "2026-01-02T11:45:00Z"
NOTIF_CUTOFF = "2026-01-02T11:50:00Z"


def _notif(
    tid: str, kind: str, number: int, updated_at: str, repo: str = "owner/repo"
) -> dict:
    """One unread notification as `GET /notifications` returns it.

    *kind* is the subject's path segment: `pulls` or `issues`.
    """
    return {
        "id": tid,
        "updated_at": updated_at,
        "repository": {"full_name": repo},
        "subject": {
            "url": f"https://api.github.com/repos/{repo}/{kind}/{number}",
            "type": "PullRequest" if kind == "pulls" else "Issue",
        },
    }


@pytest.fixture
def notifications_env(tmp_path: Path) -> dict[str, str]:
    """Fake gh/date on PATH, plus the workflow env the pre-check reads."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH_NOTIFICATIONS, date=FAKE_DATE)

    notifications = tmp_path / "notifications.json"
    notifications.write_text("[]")
    subscription_writes = tmp_path / "subscription-writes"
    subscription_writes.write_text("")

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_REPOSITORY": "owner/repo",
        "NOTIFICATIONS_JSON": str(notifications),
        "NOTIF_CUTOFF": NOTIF_CUTOFF,
        "SUBSCRIPTION_WRITES": str(subscription_writes),
    }


def _run_check(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # `bash -e` mirrors the shell GitHub Actions gives a `run:` block.
    return subprocess.run(
        [BASH, "-e", str(NOTIFICATIONS_CHECK)],
        env=env,
        capture_output=True,
        text=True,
    )


def _write_json(env: dict[str, str], key: str, value: object) -> None:
    Path(env[key]).write_text(json.dumps(value))


def test_notifications_check_reports_no_work_on_an_empty_inbox(
    notifications_env: dict[str, str],
) -> None:
    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "0"
    assert _output(notifications_env, "cutoff") == NOTIF_CUTOFF
    assert Path(notifications_env["SUBSCRIPTION_WRITES"]).read_text() == "put\n"


def test_notifications_check_counts_a_complete_cutoff_snapshot_without_acknowledging(
    notifications_env: dict[str, str],
) -> None:
    _write_json(
        notifications_env,
        "NOTIFICATIONS_JSON",
        [
            _notif("11", "issues", 7, NOTIF_SETTLED),
            _notif("22", "pulls", 8, NOTIF_SETTLED, repo="other/repo"),
            _notif("33", "issues", 9, NOTIF_CUTOFF),
            _notif("44", "issues", 10, NOTIF_FRESH),
        ],
    )

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "2"
    calls = Path(notifications_env["GH_CALLS"]).read_text()
    assert f"notifications?before={NOTIF_CUTOFF}&per_page=100" in calls
    assert "--paginate --slurp" in calls
    assert "notifications/threads/" not in calls


def test_notifications_check_enforces_repository_watching_every_cycle(
    notifications_env: dict[str, str],
) -> None:
    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "0"
    assert Path(notifications_env["SUBSCRIPTION_WRITES"]).read_text() == "put\n"
    calls = Path(notifications_env["GH_CALLS"]).read_text()
    assert "-X PUT -F subscribed=true -F ignored=false" in calls


def test_notifications_check_still_reads_the_queue_when_watching_repair_fails(
    notifications_env: dict[str, str],
) -> None:
    _write_json(
        notifications_env,
        "NOTIFICATIONS_JSON",
        [_notif("999", "issues", 7, NOTIF_SETTLED)],
    )
    notifications_env["FAIL_SUBSCRIPTION_WRITE"] = "1"

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "1"
    assert "could not enable repository watching" in result.stdout


def test_notifications_check_gives_up_cleanly_when_the_fetch_keeps_failing(
    notifications_env: dict[str, str],
) -> None:
    """The step is `bash -e`, so an untolerated `gh` failure would fail the job
    red. A cycle that can't enumerate just skips — the next one picks it up.
    """
    _write_json(
        notifications_env,
        "NOTIFICATIONS_JSON",
        [_notif("999", "issues", 7, NOTIF_SETTLED)],
    )
    notifications_env["FAIL_NOTIFS"] = "1"

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "0"


def test_notifications_check_tolerates_an_html_200(
    notifications_env: dict[str, str], tmp_path: Path
) -> None:
    """A GitHub blip can answer 200 with an HTML error page: `gh` exits zero and
    the body isn't JSON. Without the parse guard the run would carry on against
    a non-JSON snapshot and fail the step.
    """
    body = tmp_path / "body.html"
    body.write_text("<html>unicorn</html>")
    notifications_env["RAW_BODY"] = str(body)

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "0"


# ---------------------------------------------------------------------------
# review-gate.sh — the tend-review pre-boot check
# ---------------------------------------------------------------------------

REVIEW_GATE = REPO_ROOT / "generator" / "src" / "tend" / "templates" / "review-gate.sh"

HEAD_SHA = "a" * 40
OLD_SHA = "b" * 40

# `gh` stand-in for the review gate. Each fixture file holds one endpoint's
# body; the reviews and timeline calls paginate, so the fake puts the whole
# fixture on one page and lets the script's own `--jq` run over it — the filter
# is the behaviour under test.
FAKE_GH_REVIEW = (
    GH_PREAMBLE
    + r"""
# Fixtures hold one JSON document per line, the way `gh api --paginate`
# concatenates pages. Without the flag `gh` returns page one and stops, so
# serve only the first line — otherwise a script that dropped `--paginate`
# would still see every page here and the pagination tests would pass on it.
pages() {
  case " $* " in
    *" --paginate "*) cat "$1" ;;
    *) head -n1 "$1" ;;
  esac
}

case "$*" in
  *"/reviews"*)
    [ -z "${FAIL_REVIEWS:-}" ] || exit 1
    emit "$(pages "$REVIEWS_JSON" "$@")"
    ;;
  *"/timeline"*)
    [ -z "${FAIL_TIMELINE:-}" ] || exit 1
    emit "$(pages "$TIMELINE_JSON" "$@")"
    ;;
  *"/pulls/"*)
    [ -z "${FAIL_PR:-}" ] || exit 1
    # A 200 carrying something other than JSON, verbatim.
    if [ -n "${RAW_BODY:-}" ]; then cat "$RAW_BODY"; exit 0; fi
    emit "$(cat "$PR_JSON")"
    ;;
  *) exit 1 ;;
esac
"""
)


def _bot_review(
    *,
    state: str = "COMMENTED",
    body: str = "findings",
    commit_id: str = HEAD_SHA,
    submitted_at: str | None = "2026-01-02T12:00:00Z",
    login: str = "test-bot",
) -> dict:
    """One review as `GET /pulls/{n}/reviews` returns it."""
    return {
        "id": 1,
        "user": {"login": login},
        "state": state,
        "body": body,
        "commit_id": commit_id,
        "submitted_at": submitted_at,
    }


def _force_push(created_at: str) -> dict:
    return {"event": "head_ref_force_pushed", "created_at": created_at}


@pytest.fixture
def gate_env(tmp_path: Path) -> dict[str, str]:
    """Fake gh on PATH, an open PR at HEAD_SHA, no reviews, no force-pushes."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH_REVIEW)
    env = {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_REPOSITORY": "owner/repo",
        "PR": "7",
        "EVENT_ACTION": "synchronize",
        "BOT_NAME": "test-bot",
    }
    fixtures: dict[str, object] = {
        "PR_JSON": {"state": "open", "head": {"sha": HEAD_SHA}},
        "REVIEWS_JSON": [],
        "TIMELINE_JSON": [],
    }
    for key, value in fixtures.items():
        path = tmp_path / f"{key.lower()}.json"
        path.write_text(json.dumps(value))
        env[key] = str(path)
    return env


def _run_gate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # `bash -e` mirrors the shell GitHub Actions gives a `run:` block.
    return subprocess.run(
        [BASH, "-e", str(REVIEW_GATE)], env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    ("label", "reviews", "should_run"),
    [
        # What the gate exists for: the session that was running when this push
        # landed folded it in and posted its review at the live head.
        ("body at head", [_bot_review()], "false"),
        # An approval's body is empty by convention, so the state is what
        # separates it from the reply container below.
        ("empty-bodied approval", [_bot_review(state="APPROVED", body="")], "false"),
        # The recovery path: a session killed by a timeout or a cancellation
        # anchors nothing, and this run does the review it never posted.
        ("nothing posted", [], "true"),
        ("review of an earlier commit", [_bot_review(commit_id=OLD_SHA)], "true"),
        # GitHub wraps an inline reply in a synthetic zero-body COMMENTED
        # review anchored at the then-current head. It reviewed nothing.
        ("reply container", [_bot_review(body="")], "true"),
        ("another author's review", [_bot_review(login="human")], "true"),
        # The endpoint returns the caller's own unsubmitted review, and the bot
        # is the caller. `jq -r` renders its null `submitted_at` as the string
        # "null", which sorts above every real timestamp — so without the
        # explicit guard a draft nobody can see would read as the newest
        # anchor and suppress the run.
        (
            "unsubmitted pending review",
            [_bot_review(state="PENDING", submitted_at=None)],
            "true",
        ),
    ],
)
def test_review_gate_boots_unless_the_bot_reviewed_this_head(
    gate_env: dict[str, str], label: str, reviews: list[dict], should_run: str
) -> None:
    _write_json(gate_env, "REVIEWS_JSON", reviews)

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == should_run, label


def test_review_gate_reviews_a_head_an_anchor_was_rewritten_onto(
    gate_env: dict[str, str],
) -> None:
    """A force-push re-points earlier reviews' `commit_id` at the new head, so a
    review of code that no longer exists reports this head. Reading the anchor
    alone would skip the only run that reviews the rewritten commit."""
    _write_json(gate_env, "REVIEWS_JSON", [_bot_review()])
    _write_json(gate_env, "TIMELINE_JSON", [_force_push("2026-01-02T13:00:00Z")])

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"


def test_review_gate_skips_a_head_reviewed_after_the_rewrite(
    gate_env: dict[str, str],
) -> None:
    """The discount is by time, not by a force-push having ever happened: a PR
    force-pushed and then reviewed is covered like any other. Both force-push
    events are read, not just the first — an anchor is stale when a rewrite
    postdates it."""
    _write_json(gate_env, "REVIEWS_JSON", [_bot_review()])
    _write_json(
        gate_env,
        "TIMELINE_JSON",
        [_force_push("2026-01-02T09:00:00Z"), _force_push("2026-01-02T11:00:00Z")],
    )

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "false"


@pytest.mark.parametrize("action", ["opened", "ready_for_review", "labeled"])
def test_review_gate_admits_every_other_event_without_asking(
    gate_env: dict[str, str], action: str
) -> None:
    """`opened` has no prior run, and `ready_for_review` sets the session's
    `FORCE_FULL_REVIEW`, which bypasses the already-reviewed check so a draft's
    COMMENT-only pass is redone in full. An action the gate doesn't name falls
    through to a run — the fail-open direction, so widening `types:` in an
    override can't silently start skipping. All decided before any API call."""
    gate_env["EVENT_ACTION"] = action
    _write_json(gate_env, "REVIEWS_JSON", [_bot_review()])

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"
    assert not Path(gate_env["GH_CALLS"]).exists()


def test_review_gate_skips_a_reopen_of_an_already_reviewed_head(
    gate_env: dict[str, str],
) -> None:
    """Reopening changes no code. The session's step 1 stops on an
    already-reviewed head — only `ready_for_review` sets `FORCE_FULL_REVIEW` —
    so admitting `reopened` boots an agent that posts nothing."""
    gate_env["EVENT_ACTION"] = "reopened"
    _write_json(gate_env, "REVIEWS_JSON", [_bot_review()])

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "false"


def test_review_gate_skips_a_pr_that_is_no_longer_open(
    gate_env: dict[str, str],
) -> None:
    """A push to a merged or closed PR's branch still fires `synchronize`, and a
    review posted after the close is an artifact nobody asked for. `closed`
    covers merged too — `GET /pulls/{n}` reports a merged PR as closed, with
    the merge in a separate field."""
    _write_json(gate_env, "PR_JSON", {"state": "closed", "head": {"sha": HEAD_SHA}})

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "false"


@pytest.mark.parametrize(
    "failure", ["FAIL_PR", "FAIL_REVIEWS", "FAIL_TIMELINE", "RAW_BODY"]
)
def test_review_gate_fails_open(
    gate_env: dict[str, str], tmp_path: Path, failure: str
) -> None:
    """A redundant agent run beats a silently skipped review, and the step is
    `bash -e`, so an untolerated `gh` or `jq` failure would fail the job red.
    `RAW_BODY` is the blip that answers 200 with an HTML error page: `gh` exits
    zero and the body isn't JSON."""
    _write_json(gate_env, "REVIEWS_JSON", [_bot_review()])
    if failure == "RAW_BODY":
        body = tmp_path / "body.html"
        body.write_text("<html>unicorn</html>")
        gate_env[failure] = str(body)
    else:
        gate_env[failure] = "1"

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"


def test_review_gate_fails_open_on_a_pr_payload_missing_its_head(
    gate_env: dict[str, str],
) -> None:
    """Well-formed JSON that isn't a PR object. Every read of it goes through
    the same guard, so a shape the script can't use fails open like any other
    bad body. `head` is a string here rather than absent: indexing it raises a
    jq *type* error, which `jq -r` alone exits non-zero on, so read outside the
    guard it would fail the step red — the one direction the guard rules out."""
    _write_json(gate_env, "PR_JSON", {"state": "open", "head": "not-an-object"})
    _write_json(gate_env, "REVIEWS_JSON", [_bot_review()])

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"


def test_review_gate_reads_every_page_of_reviews(gate_env: dict[str, str]) -> None:
    """The reviews list is paginated, and a review that anchors the head can sit
    on any page — GitHub orders them oldest-first, so on a long-lived PR the
    newest is the one that spills. Fetch every page: without `--paginate` the
    gate sees page one, finds no anchor, and re-reviews a reviewed head."""
    Path(gate_env["REVIEWS_JSON"]).write_text(
        json.dumps([_bot_review(commit_id=OLD_SHA)])
        + "\n"
        + json.dumps([_bot_review()])
    )

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "false"


def test_review_gate_discounts_by_the_newest_anchor_not_the_oldest(
    gate_env: dict[str, str],
) -> None:
    """`--jq` runs per page, so both lists are reduced in the shell. The anchor
    side has to take the *newest* review: a PR reviewed, force-pushed, and
    reviewed again carries an anchor on each side of the rewrite, and reducing
    to the older one reads the head as unreviewed and re-reviews it."""
    _write_json(
        gate_env,
        "REVIEWS_JSON",
        [
            _bot_review(submitted_at="2026-01-02T09:00:00Z"),
            _bot_review(submitted_at="2026-01-02T13:00:00Z"),
        ],
    )
    _write_json(gate_env, "TIMELINE_JSON", [_force_push("2026-01-02T12:00:00Z")])

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "false"


def test_review_gate_reviews_again_when_the_rewrite_ties_the_anchor(
    gate_env: dict[str, str],
) -> None:
    """Timestamps are second-resolution, so a rewrite in the same second as the
    review it may have invalidated is genuinely ambiguous. Strictly newer, so
    the tie resolves into a run rather than a skipped review."""
    _write_json(
        gate_env,
        "REVIEWS_JSON",
        [_bot_review(submitted_at="2026-01-02T12:00:00Z")],
    )
    _write_json(gate_env, "TIMELINE_JSON", [_force_push("2026-01-02T12:00:00Z")])

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"


def test_review_gate_reads_every_page_of_the_timeline(
    gate_env: dict[str, str],
) -> None:
    """Same per-page hazard on the other fetch, and it fails the dangerous way:
    a rewrite recorded past page one would go unseen and the stale anchor would
    skip the run that reviews the rewritten head."""
    _write_json(gate_env, "REVIEWS_JSON", [_bot_review()])
    Path(gate_env["TIMELINE_JSON"]).write_text(
        json.dumps([_force_push("2026-01-02T09:00:00Z")])
        + "\n"
        + json.dumps([_force_push("2026-01-02T13:00:00Z")])
    )

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"


def test_sensitive_paths_are_documented_where_adopters_read_them() -> None:
    """The pinned root paths are a security claim, so the docs have to name all
    of them. The list drifted once already: `.husky` sat in the script and in
    the threat model while the README enumerated four of the five, understating
    the surface for the only audience that reads the README.
    """
    script = RESTORE_SENSITIVE_CONFIG.read_text()
    sensitive = re.search(r"^SENSITIVE=\((.*?)\)$", script, re.M)
    assert sensitive, "SENSITIVE array not found — did the script's shape change?"
    paths = sensitive.group(1).split()
    assert paths, "SENSITIVE is empty"

    # Scope to the paragraph making the claim; a stray mention elsewhere in the
    # file (README's own repo layout, say) must not satisfy it.
    readme = REPO_ROOT / "README.md"
    claim = re.search(r"\*\*Config pinning\*\*.*?(?=\n\n)", readme.read_text(), re.S)
    assert claim, "README's config-pinning paragraph not found"

    threat_model = (REPO_ROOT / "docs" / "security-model.md").read_text()
    for path in paths:
        assert f"`{path}`" in claim.group(0), f"{path} missing from README"
        assert f"`{path}`" in threat_model, f"{path} missing from security-model.md"
