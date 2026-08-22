"""Tests for the workflow step bodies shipped as shell scripts.

Two homes, one testing need: the composite actions' shared steps
(shared/steps/*.sh) run as `bash <script>` inside both harness actions, and
the generator's template scripts (generator/src/tend/templates/*.sh) are
inlined into generated workflows. In both, a non-zero exit fails the step, and
shellcheck (pre-commit) can't catch runtime behaviour; this is the repo's only
Python suite, so the tests live here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from tests import BASH, GH_PREAMBLE, fake_bin, tool_path

REPO_ROOT = Path(__file__).resolve().parents[2]
MARK_NOTIFICATION_READ = REPO_ROOT / "shared" / "steps" / "mark-notification-read.sh"
COMPUTE_TOKEN_USAGE = REPO_ROOT / "shared" / "steps" / "compute-token-usage.sh"
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
    symlink. Skips `.git/` and the `.claude-pr/` snapshot."""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in {".git", ".claude-pr"}:
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
    tmp_path: Path, base: dict[str, str], tamper: Callable[[Path], None]
) -> tuple[Path, Path]:
    """A fork PR's checkout as `actions/checkout` leaves it: a clone of an
    origin holding *base*, with *tamper*'s changes committed on top and
    `origin/main` at the base. Returns (repo, event)."""
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
                    "base": {"ref": "main"},
                    "head": {"repo": {"fork": True}},
                },
                "repository": {"default_branch": "main"},
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
    """Same contract under the Claude harness, plus its own two: the fork's root
    files are snapshotted to `.claude-pr/` for review skills to read, and the
    revert stays out of the index so it can't ride into a commit the agent
    makes later."""
    repo, outside, event = _tampered_checkout(tmp_path)

    result = _pin(RESTORE_SENSITIVE_CONFIG, repo, event)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(repo) == _PINNED
    assert _tree(outside) == _OUTSIDE
    assert (repo / ".claude-pr" / "CLAUDE.md").read_text() == "EVIL root\n"
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


# `gh api` stand-in. Records every invocation so a test can assert which calls
# the script made, and fails the run-metadata fetch when FAIL_RUN_META is set.
FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$2" in
  repos/*/actions/runs/*)
    [ -n "${FAIL_RUN_META:-}" ] && exit 1
    echo "$FAKE_RUN_STARTED_AT"
    ;;
  notifications)
    cat "$NOTIFICATIONS_JSON"
    ;;
  notifications/threads/*)
    ;;
  *)
    exit 1
    ;;
esac
"""


@pytest.fixture
def gh_env(tmp_path: Path) -> dict[str, str]:
    """A fake `gh` on PATH plus the Actions env the script reads."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH)

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"issue": {"number": 7}}))

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "12345",
        # Deliberately not `RUN_STARTED_AT`: the script assigns that name, and
        # an inherited value would let the happy-path tests pass even if the
        # fetched timestamp were never used.
        "FAKE_RUN_STARTED_AT": "2026-01-02T00:00:00Z",
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(MARK_NOTIFICATION_READ)],
        env=env,
        capture_output=True,
        text=True,
    )


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


# The Run cell of a row generated under the fixtures' GITHUB_RUN_ID. What a
# carried-over row is recognised by, on either record.
RUN_LINK = "[workflow run](https://github.com/owner/repo/actions/runs/12345)"


def _notifications(tmp_path: Path, updated_at: str) -> str:
    """One unread notification for issue 7 of owner/repo."""
    path = tmp_path / "notifications.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "999",
                    "updated_at": updated_at,
                    "subject": {
                        "url": "https://api.github.com/repos/owner/repo/issues/7"
                    },
                }
            ]
        )
    )
    return str(path)


def test_mark_notification_read_tolerates_run_metadata_failure(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """A transient failure fetching `run_started_at` must not fail the step.

    The script runs under `set -e`, so an unguarded `gh api` there aborts it
    non-zero. The step is gated on `if: success()` in both harness actions, so
    that exit turns a fully-successful agent run into a red job. The correct
    disposition is to skip this cycle and leave the thread unread — the
    scheduled tend-notifications poll picks it up.
    """
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-01-01T00:00:00Z")
    gh_env["FAIL_RUN_META"] = "1"

    result = _run(gh_env)

    assert result.returncode == 0, (
        f"script aborted on a transient run-metadata error (exit "
        f"{result.returncode}); stderr:\n{result.stderr}"
    )
    # Without the timestamp the `updated_at <= started` guard can't be
    # evaluated, so nothing may be marked read.
    assert not any("-X PATCH" in c for c in _calls(gh_env)), (
        "marked a thread read without knowing when the run started"
    )


def test_mark_notification_read_treats_a_null_timestamp_as_absent(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """A 200 whose body lacks `run_started_at` must be handled like a failure.

    `gh --jq` prints the literal `null` for a missing field, which is non-empty
    and so survives the `-z` guard. It then reaches the jq comparison as a
    string, and every ISO-8601 timestamp sorts before `null` by codepoint — so
    the `updated_at <= $started` filter matches every thread and the run marks
    read the mid-run activity the guard exists to preserve. The notification
    here is dated two months *after* the run, so a PATCH can only come from
    that inversion.
    """
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-03-01T00:00:00Z")
    gh_env["FAKE_RUN_STARTED_AT"] = "null"

    result = _run(gh_env)

    assert result.returncode == 0, result.stderr
    assert not any("-X PATCH" in c for c in _calls(gh_env)), (
        "marked a thread read against a `null` run_started_at"
    )


def test_mark_notification_read_marks_thread_predating_the_run(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """The happy path still marks a thread whose activity predates the run."""
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-01-01T00:00:00Z")

    result = _run(gh_env)

    assert result.returncode == 0, result.stderr
    assert "api notifications/threads/999 -X PATCH" in _calls(gh_env)


def test_mark_notification_read_leaves_activity_newer_than_the_run(
    tmp_path: Path, gh_env: dict[str, str]
) -> None:
    """Activity that arrived after the run started stays unread."""
    gh_env["NOTIFICATIONS_JSON"] = _notifications(tmp_path, "2026-03-01T00:00:00Z")

    result = _run(gh_env)

    assert result.returncode == 0, result.stderr
    assert not any("-X PATCH" in c for c in _calls(gh_env))


# --- compute-token-usage.sh -------------------------------------------------
#
# Fixtures below mirror the shapes observed in real uploaded artifacts. Three
# properties drive the tests:
#
# 1. Both files record each assistant message roughly twice, so any sum has to
#    deduplicate by `.message.id` or it lands ~2x high.
# 2. The stream-json's assistant events are non-final (`stop_reason: null`):
#    their `usage.output_tokens` is the message-start placeholder (single
#    digits), not the finished count. Only the session JSONL carries final
#    per-message usage. Reconstructing from the stream-json therefore
#    under-counts output by orders of magnitude, while input and cache fields
#    — known at message start — happen to match.
# 3. A session that ran a `Task` has a second transcript under
#    `<session-id>/subagents/`, whose usage the `result` event does not count.


def _assistant(msg_id: str, usage: dict[str, int], *, final: bool) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "stop_reason": "end_turn" if final else None,
            "usage": usage,
        },
    }


def _ndjson(path: Path, lines: list[dict[str, object]]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


# Final per-message usage, as the session JSONL records it.
FINAL_USAGE = [
    {
        "input_tokens": 10,
        "output_tokens": 3000,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 20000,
    },
    {
        "input_tokens": 5,
        "output_tokens": 1500,
        "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 40000,
    },
]
# The same two messages as the stream-json emits them: input/cache identical,
# output still at its message-start placeholder.
STREAM_USAGE = [dict(u, output_tokens=6) for u in FINAL_USAGE]


# A `Task` subagent's own transcript, which real artifacts carry alongside the
# session it belongs to. Its usage is not in the `result` event, so nothing
# here may reach the totals.
SUBAGENT_USAGE = {
    "input_tokens": 300,
    "output_tokens": 7000,
    "cache_creation_input_tokens": 40000,
    "cache_read_input_tokens": 900000,
}


def _session_jsonl(logs_dir: Path) -> Path:
    """A cancelled session's JSONL: real usage, each message duplicated.

    Writes the subagent transcript beside it too — `<session>/subagents/` is
    how Claude Code lays a `Task` out on disk, and `cp -a .../projects/.`
    copies the subtree into LOGS_DIR.
    """
    project = logs_dir / "-home-runner-work-repo-repo"
    project.mkdir(parents=True, exist_ok=True)
    lines: list[dict[str, object]] = [{"type": "user"}]
    for i, usage in enumerate(FINAL_USAGE):
        entry = _assistant(f"msg_{i}", usage, final=True)
        lines += [entry, dict(entry), {"type": "user"}]
    lines.append({"type": "user"})

    subagents = project / "session" / "subagents"
    subagents.mkdir(parents=True, exist_ok=True)
    _ndjson(
        subagents / "agent-a1b2c3.jsonl",
        [
            {"type": "user"},
            _assistant("msg_sub", SUBAGENT_USAGE, final=True),
            {"type": "user"},
        ],
    )
    return _ndjson(project / "session.jsonl", lines)


def _cancelled_stream(tmp_path: Path) -> Path:
    """Stream-json for the same session: assistant events, no `result`."""
    lines: list[dict[str, object]] = [{"type": "system"}]
    for i, usage in enumerate(STREAM_USAGE):
        entry = _assistant(f"msg_{i}", usage, final=False)
        lines += [entry, dict(entry), {"type": "user"}]
    return _ndjson(tmp_path / "stream.json", lines)


def _usage(tmp_path: Path, *, stream: Path | None, logs_dir: Path) -> dict[str, object]:
    result = subprocess.run(
        [BASH, str(COMPUTE_TOKEN_USAGE)],
        env={
            "PATH": tool_path(),
            "MODEL": "opus",
            "LOGS_DIR": str(logs_dir),
            "STREAM_JSON": str(stream) if stream else "",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_token_usage_reconstructs_a_cancelled_session(tmp_path: Path) -> None:
    """A cancelled session must be accounted from its session JSONL.

    `tend-review` runs with `cancel-in-progress: true`, so cancellation is
    routine — and a cancelled session never emits a `type: "result"` event.
    The step is `if: always()`, so it still writes token-usage.json and still
    uploads the artifact; only the accounting is lost. Reporting zeros for a
    run that did real work (and may already have posted a review) biases every
    downstream total by the cancellation rate.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    assert usage["output_tokens"] == 4500, (
        f"cancelled session reported output_tokens={usage['output_tokens']}; "
        "the session JSONL records 4500 across two messages"
    )
    assert usage["input_tokens"] == 15
    assert usage["cache_creation_input_tokens"] == 1500
    assert usage["cache_read_input_tokens"] == 60000
    # Three `user` lines bracket the two assistant turns; num_turns counts the
    # turns between them.
    assert usage["turns"] == 3
    assert usage["partial"] is True, (
        "a reconstructed total must be distinguishable from a run that "
        "genuinely cost nothing"
    )


def test_token_usage_ignores_stream_json_placeholder_output(tmp_path: Path) -> None:
    """The fallback must not sum the stream-json's non-final assistant events.

    They carry `stop_reason: null` and a message-start `output_tokens`, so
    summing them under-counts output by orders of magnitude while input and
    cache fields still match — a wrong number that looks plausible.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    stream_sum = sum(u["output_tokens"] for u in STREAM_USAGE)
    assert usage["output_tokens"] != stream_sum, (
        "summed the stream-json's placeholder output_tokens"
    )


def test_token_usage_ignores_subagent_transcripts(tmp_path: Path) -> None:
    """Subagent transcripts must not be slurped into the reconstruction.

    Every `Task` writes its own `<session>/subagents/agent-*.jsonl`, but the
    `result` event this fallback stands in for counts only the main loop.
    Summing both inflates each field — turns roughly doubles — so a partial
    run would no longer be comparable with a complete one.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    assert usage["output_tokens"] == 4500, "summed the subagent's output_tokens"
    assert usage["cache_read_input_tokens"] == 60000, "summed the subagent's cache"
    assert usage["turns"] == 3, "counted the subagent's `user` lines as turns"


def test_token_usage_prefers_result_events_when_present(tmp_path: Path) -> None:
    """A completed session still reports straight from its `result` events."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)
    stream = _ndjson(
        tmp_path / "stream.json",
        [
            _assistant("msg_0", STREAM_USAGE[0], final=False),
            {
                "type": "result",
                "num_turns": 14,
                "total_cost_usd": 1.2563179999999998,
                "usage": {
                    "input_tokens": 23,
                    "output_tokens": 9406,
                    "cache_creation_input_tokens": 62655,
                    "cache_read_input_tokens": 789006,
                },
            },
        ],
    )

    usage = _usage(tmp_path, stream=stream, logs_dir=logs_dir)

    assert usage["output_tokens"] == 9406
    assert usage["turns"] == 14
    assert usage["cost_usd"] == 1.26
    assert usage["partial"] is False


def test_token_usage_survives_a_truncated_final_line(tmp_path: Path) -> None:
    """A half-written line costs that line, not the run's whole accounting.

    A cancelled process can be killed mid-append, leaving its session JSONL
    ending in a partial entry. `jq -s` aborts the file on the first parse
    error and the `|| echo ''` swallows it, which would drop the run into the
    "agent never ran" branch — republishing the all-zero `partial: false`
    payload this fallback exists to replace, now indistinguishable from a
    genuine preflight no-op.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    session = _session_jsonl(logs_dir)
    session.write_text(session.read_text() + '{"type":"assistant","mess')

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    assert usage["output_tokens"] == 4500, "a truncated tail zeroed the totals"
    assert usage["turns"] == 3
    assert usage["partial"] is True


def test_token_usage_survives_a_truncated_line_beside_a_second_session(
    tmp_path: Path,
) -> None:
    """A truncated file must not take the next file's first line with it.

    `jq -R -s` concatenates its inputs into one string before `split("\\n")`
    runs, so a file ending without a newline would join its partial last line
    to the next file's first line and `fromjson?` would drop the pair. The
    files are read through `awk 1`, which terminates each one.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    session = _session_jsonl(logs_dir)
    session.write_text(session.read_text() + '{"type":"assistant","mess')

    # Sorts after the truncated file, so it is the one glued onto its tail.
    second = logs_dir / "-home-runner-work-repo-repo2"
    second.mkdir()
    _ndjson(
        second / "session.jsonl",
        [_assistant("msg_second", FINAL_USAGE[1], final=True), {"type": "user"}],
    )

    usage = _usage(tmp_path, stream=_cancelled_stream(tmp_path), logs_dir=logs_dir)

    assert usage["output_tokens"] == 6000, "lost the second session's first message"
    # p2 contributes its opening prompt and no turn of its own. The subtraction
    # is per session, so pooling the files must not count that prompt as one.
    assert usage["turns"] == 3, "counted the second session's prompt as a turn"
    assert usage["partial"] is True


def test_token_usage_survives_a_truncated_stream_json_line(tmp_path: Path) -> None:
    """The same truncation on the stream-json must not lose a `result` event.

    Falling through to the session JSONL would still report the tokens, but as
    `partial` with an unknown cost — a needless downgrade when the result event
    itself parsed fine.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    _session_jsonl(logs_dir)
    stream = _ndjson(
        tmp_path / "stream.json",
        [
            {
                "type": "result",
                "num_turns": 14,
                "total_cost_usd": 1.25,
                "usage": {"input_tokens": 23, "output_tokens": 9406},
            },
        ],
    )
    stream.write_text(stream.read_text() + '{"type":"resu')

    usage = _usage(tmp_path, stream=stream, logs_dir=logs_dir)

    assert usage["output_tokens"] == 9406
    assert usage["cost_usd"] == 1.25
    assert usage["partial"] is False


def test_token_usage_reports_zero_when_the_agent_never_ran(tmp_path: Path) -> None:
    """No stream and no session JSONL is a genuine zero, not a partial total.

    A run that dies in preflight really did cost nothing; flagging it partial
    would push a fabricated unknown into the reports.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    usage = _usage(tmp_path, stream=None, logs_dir=logs_dir)

    assert usage["output_tokens"] == 0
    assert usage["cost_usd"] == 0
    assert usage["partial"] is False


RATE_LIMIT_PREFLIGHT = REPO_ROOT / "shared" / "steps" / "rate-limit-preflight.sh"

# `gh` stand-in for the rate-limit preflight. Unlike FAKE_GH it runs the
# script's own `--jq` expression against a fixture with real jq, because that
# filter *is* the behaviour under test: which closes count as an approval. A
# fake that returned a pre-filtered actor list would assert nothing.
FAKE_GH_RATE_LIMIT = (
    GH_PREAMBLE
    + r"""
case "$1" in
  api)
    case "$2" in
      *"/events?"*) emit "$(cat "$TIMELINE_JSON")" ;;
      user)
        [ -n "${FAIL_WHOAMI:-}" ] && exit 1
        emit "{\"login\":\"tend-agent\",\"id\":${FAKE_BOT_ID}}"
        ;;
      *"/pulls?"*)
        # Built through jq so the script's own burst filter is what counts them.
        emit "$(jq -nc --argjson n "${FAKE_RECENT_PRS:-0}" \
          '[range($n) | {user: {login: "tend-agent"}, created_at: "2099-01-01T00:00:00Z"}]')"
        ;;
      *"/issues?creator="*) emit '[]' ;;
      repos/*/issues/*)
        # The reconciler's primary-key probe. Serves whatever the fixture put
        # at that number, so the script's own `--jq` decides whether it counts.
        emit "$(jq -c --argjson n "${2##*/}" \
          'map(select(.number == $n)) | .[0] // {"number":0}' "$PROBE_ISSUES_JSON")"
        ;;
      "search/issues?"*)
        # The baseline query is the one carrying a `created:from..to` range.
        case "$2" in
          *".."*) emit "{\"total_count\":${FAKE_PAST_POSTS}}" ;;
          *) emit "{\"total_count\":${FAKE_TODAY_POSTS}}" ;;
        esac
        ;;
      *) exit 1 ;;
    esac
    ;;
  issue)
    case "$2" in
      list)
        # Fail the list calls in [FROM, UNTIL], so the spike block's two reads
        # can be failed in any combination: FROM=1 alone fails both, FROM=2
        # spares the first, and FROM=1 UNTIL=1 spares the re-read. UNTIL is
        # unbounded by default, which keeps "the list is simply down" open
        # ended rather than pinned to an exact call count.
        if [ -n "${FAIL_ISSUE_LIST_FROM:-}" ]; then
          n=$(( $(cat "$LIST_CALLS" 2>/dev/null || echo 0) + 1 ))
          echo "$n" > "$LIST_CALLS"
          if [ "$n" -ge "$FAIL_ISSUE_LIST_FROM" ] \
            && { [ -z "${FAIL_ISSUE_LIST_UNTIL:-}" ] || [ "$n" -le "$FAIL_ISSUE_LIST_UNTIL" ]; }; then
            exit 1
          fi
        fi
        emit "$(cat "$PAUSE_ISSUES_JSON")"
        ;;
      create)
        # An `if` rather than `[ ... ] && exit 1`: with nothing after it, the
        # failed test would become the branch's status and every create would
        # report failure.
        if [ -n "${FAIL_ISSUE_CREATE:-}" ]; then exit 1; fi
        # `gh issue create` prints the new issue's URL; the reconciler reads its
        # number off the end of it.
        echo "https://github.com/owner/repo/issues/${FAKE_NEW_ISSUE}"
        ;;
      view) emit "$(cat "$KEEPER_JSON")" ;;
      comment)
        if [ -n "${FAIL_ISSUE_COMMENT:-}" ]; then exit 1; fi
        # Comment bodies arrive on stdin (`-F -`), not in the args, so they are
        # captured rather than dropped: the carry-over row is asserted on.
        cat >> "$COMMENT_BODIES"
        ;;
      reopen | close) ;;
      *) exit 1 ;;
    esac
    ;;
  label) ;;
  *) exit 1 ;;
esac
"""
)

# The script is written for the Ubuntu runners' GNU date; macOS ships BSD
# date, which has no `-d`. Fixed values also make the day-scoping assertions
# deterministic: "today" is 2026-01-02.
# The relative offsets come first: every call also carries a format string, so
# matching that branch first would collapse them all onto one timestamp.
FAKE_DATE = r"""#!/usr/bin/env bash
case "$*" in
  *"30 minutes ago"*) echo "2026-01-02T11:30:00Z" ;;
  *"20 minutes ago"*) echo "2026-01-02T11:40:00Z" ;;
  *"10 minutes ago"*) echo "2026-01-02T11:50:00Z" ;;
  *"yesterday"*) echo "2026-01-01" ;;
  *"6 days ago"*) echo "2025-12-27" ;;
  *"%Y-%m-%dT%H:%M:%SZ"*) echo "2026-01-02T12:00:00Z" ;;
  *) echo "2026-01-02" ;;
esac
"""

# The preflight jitters before its check-then-act, and the notifications
# pre-check backs off between fetch attempts; real sleeps would add up.
FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"

TODAY = "2026-01-02"
BOT_ID = 4242
PAUSE_TITLE = "Bot rate limit reached"
PAUSE_LABEL = "tend-rate-limit"
# The label goes on when the preflight files the issue; approvals are closes
# after that moment.
LABELLED_AT = f"{TODAY}T08:00:00Z"


def _probe_issue(
    number: int,
    *,
    title: str,
    label: str,
    login: str = "tend-agent",
    state: str = "open",
) -> dict:
    """One issue as `GET /issues/{n}` returns it, for the reconciler's probe."""
    return {
        "number": number,
        "title": title,
        "state": state,
        "user": {"login": login},
        "labels": [{"name": label}],
    }


def _closed_event(
    login: str,
    actor_type: str = "User",
    day: str = TODAY,
    actor_id: int = 99,
) -> dict:
    return {
        "event": "closed",
        "actor": {"login": login, "id": actor_id, "type": actor_type},
        "created_at": f"{day}T09:00:00Z",
    }


@pytest.fixture
def rate_limit_env(tmp_path: Path) -> dict[str, str]:
    """Fake gh/date/sleep on PATH, plus the Actions env the preflight reads."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH_RATE_LIMIT, date=FAKE_DATE, sleep=FAKE_SLEEP)

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 851}}))

    timeline = tmp_path / "timeline.json"
    timeline.write_text("[]")
    pause_issues = tmp_path / "pause-issues.json"
    pause_issues.write_text("[]")
    probe_issues = tmp_path / "probe-issues.json"
    probe_issues.write_text("[]")
    keeper = tmp_path / "keeper.json"
    keeper.write_text('{"body": "", "comments": []}')
    (tmp_path / "comment-bodies.txt").write_text("")

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "LIST_CALLS": str(tmp_path / "list-calls"),
        "TIMELINE_JSON": str(timeline),
        "PAUSE_ISSUES_JSON": str(pause_issues),
        "PROBE_ISSUES_JSON": str(probe_issues),
        "KEEPER_JSON": str(keeper),
        "COMMENT_BODIES": str(tmp_path / "comment-bodies.txt"),
        "FAKE_NEW_ISSUE": "42",
        # past=15 puts the base limit at 10 + 15/3 = 15.
        "FAKE_PAST_POSTS": "15",
        "FAKE_TODAY_POSTS": "10",
        "FAKE_RECENT_PRS": "0",
        "FAKE_BOT_ID": str(BOT_ID),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_EVENT_PATH": str(event),
    }


def _run_preflight(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(RATE_LIMIT_PREFLIGHT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _approve(
    env: dict[str, str],
    *events: dict,
    issue: int = 42,
    labelled_at: str = LABELLED_AT,
) -> None:
    """Put a pause issue on the label, labelled then carrying `events`."""
    Path(env["PAUSE_ISSUES_JSON"]).write_text(
        json.dumps([{"number": issue, "title": PAUSE_TITLE}])
    )
    labelled = {
        "event": "labeled",
        "label": {"name": "tend-rate-limit"},
        "created_at": labelled_at,
    }
    Path(env["TIMELINE_JSON"]).write_text(json.dumps([labelled, *events]))


def test_rate_limit_passes_under_the_limit(rate_limit_env: dict[str, str]) -> None:
    """Under the base limit nothing is looked up and nothing is filed."""
    result = _run_preflight(rate_limit_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(rate_limit_env)
    assert not any(c.startswith("issue ") for c in calls), (
        f"touched an issue while under the limit: {calls}"
    )


def test_rate_limit_files_an_issue_when_unapproved(
    rate_limit_env: dict[str, str],
) -> None:
    """Over the limit with no approval: refuse, and file the issue that says so."""
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert any(c.startswith("issue create") for c in _calls(rate_limit_env))


def test_rate_limit_says_so_when_the_issue_cannot_be_filed(
    rate_limit_env: dict[str, str],
) -> None:
    """A failed create must not be reported as a filed issue.

    `set -e` does not reach inside a command substitution, so the failure runs
    on to the function's trailing `printf` and the caller reads success with an
    empty number. The run is refused either way; what is lost is the notice —
    and the annotation used to print a literal `#?`, sending a maintainer after
    an issue that does not exist while the bot stays halted for the UTC day.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    rate_limit_env["FAIL_ISSUE_CREATE"] = "1"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert "could not be filed" in result.stdout
    assert "#?" not in result.stdout


def test_rate_limit_names_the_issue_when_the_index_lags(
    rate_limit_env: dict[str, str],
) -> None:
    """Created while the issue index lagged: still name the number.

    The reconcile reads the number off the create's own URL rather than out of
    a list, so a lagging index no longer costs the annotation its number — the
    state this used to cover (filed, number unknown) is unreachable now. Still
    distinct from a failed create: the issue is there to be closed, so the
    annotation offers the approval route either way.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    # The lag is the subject, so it is set here rather than left to the
    # fixture's default: the create succeeds, and the list it reconciles
    # against still does not show the issue.
    Path(rate_limit_env["PAUSE_ISSUES_JSON"]).write_text("[]")

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert f"#{rate_limit_env['FAKE_NEW_ISSUE']}" in result.stdout
    assert "could not be filed" not in result.stdout
    assert "#?" not in result.stdout


def test_rate_limit_keeps_its_annotation_when_the_row_cannot_be_appended(
    rate_limit_env: dict[str, str],
) -> None:
    """A failed comment must not cost the run its annotation.

    The append path is the common one — every refusal after the first in an
    incident takes it — and a bare pipeline under `set -e` aborts the script on
    it, so the run leaves no trace at all. The row is the lesser loss: the issue
    exists, so the annotation can still say what to close.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    rate_limit_env["FAIL_ISSUE_COMMENT"] = "1"
    # The issue exists and carries the label, but nothing has approved it.
    _approve(rate_limit_env)

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert "Refused runs are listed in #42" in result.stdout


def test_rate_limit_files_nothing_when_the_issue_list_cannot_be_read(
    rate_limit_env: dict[str, str],
) -> None:
    """A failed list read must not be taken for "no issue exists".

    Both readings are the empty string, and acting on the wrong one files a
    second pause issue. The reconcile cannot merge that one away: it probes the
    ten numbers under the issue it just filed, and an already-open pause issue
    is normally far older. The duplicate then costs an approval outright — the
    lookup resolves to the lowest-numbered issue, so a maintainer closing the
    newer one, which is the issue this run's annotation names, approves nothing.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    # An issue is already open; the point is that this run cannot see it.
    _approve(rate_limit_env)
    rate_limit_env["FAIL_ISSUE_LIST_FROM"] = "1"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert not any(c.startswith("issue create") for c in calls), calls
    assert "could not be read" in result.stdout
    assert "could not be filed" not in result.stdout


def test_rate_limit_still_files_when_only_the_re_read_fails(
    rate_limit_env: dict[str, str],
) -> None:
    """A failed re-read must not suppress the file the first read cleared.

    The two reads rule out different things. The first excludes an already-open
    issue of any age, which is the duplicate worth avoiding; the re-read after
    the jitter only narrows the seconds-wide sibling race, and the reconcile's
    downward probe catches that anyway. Holding off here would pause the bot
    with no issue at all — the outcome opening one exists to avoid — and point
    the maintainer at an issue this run's own first read established isn't
    there.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    # Nothing open, so the first read is a clean "none"; only the re-read fails.
    rate_limit_env["FAIL_ISSUE_LIST_FROM"] = "2"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert any(c.startswith("issue create") for c in _calls(rate_limit_env))
    assert "could not be read" not in result.stdout


def test_rate_limit_files_when_only_the_first_read_fails(
    rate_limit_env: dict[str, str],
) -> None:
    """The re-read's verdict counts when the first read never landed.

    The mirror of the case above, and the reason the re-read raises the flag
    rather than merely leaving it alone. Without that raise the run refuses,
    files nothing, and points the maintainer at an open issue the re-read had
    just established isn't there — the same dead end from the other side.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    # Only the first read fails; the re-read comes back clean and empty.
    rate_limit_env["FAIL_ISSUE_LIST_FROM"] = "1"
    rate_limit_env["FAIL_ISSUE_LIST_UNTIL"] = "1"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert any(c.startswith("issue create") for c in _calls(rate_limit_env))
    assert "could not be read" not in result.stdout


def test_rate_limit_human_close_doubles_the_ceiling(
    rate_limit_env: dict[str, str],
) -> None:
    """One close by a person takes the ceiling from 15 to 30."""
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("maintainer"))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 0, result.stderr
    assert "ceiling 30" in result.stdout


def test_rate_limit_bot_cannot_approve_itself(rate_limit_env: dict[str, str]) -> None:
    """The security property: the bot closing its own issue is not an approval.

    The bot has `issues: write` and authors this issue, so it *can* close it.
    What stops that being self-approval is this filter, not an instruction in
    a prompt — which is why it is asserted against the real jq expression.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("tend-agent", actor_id=BOT_ID))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, (
        "the bot approved itself by closing its own pause issue"
    )


def test_rate_limit_renamed_bot_still_cannot_approve(
    rate_limit_env: dict[str, str],
) -> None:
    """A renamed account is still the bot.

    The account is an ordinary user account, so the type check does nothing for
    it and identifying it is the whole control. Matching on a name would fail
    open the moment the account were renamed: an actor matching nothing reads
    as an approving person. Here the close carries an unfamiliar login and the
    bot's id.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(
        rate_limit_env,
        _closed_event("tend-agent-renamed", actor_id=BOT_ID),
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, (
        "a rename let the bot approve itself; the check is matching on a name"
    )


def test_rate_limit_reconciler_keeps_only_what_the_preflight_filed(
    rate_limit_env: dict[str, str],
) -> None:
    """The reconciler nominates its keeper on the anchor's predicate.

    On the label alone, any lower-numbered issue carrying it outranks the
    record just filed, which is then closed as that issue's duplicate — the
    refused-run rows and the `::error::` end up pointing at different issues.
    The reconciler probes numbers below its own one at a time, so the whole
    predicate — author, title, label, still open — runs per issue; each of
    these sits inside the probe window failing exactly one of them.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    Path(rate_limit_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps(
            [
                _probe_issue(
                    41, title="Something a maintainer labelled", label=PAUSE_LABEL
                ),
                _probe_issue(40, title=PAUSE_TITLE, label="unrelated-label"),
                _probe_issue(39, title=PAUSE_TITLE, label=PAUSE_LABEL, login="someone"),
                _probe_issue(38, title=PAUSE_TITLE, label=PAUSE_LABEL, state="closed"),
            ]
        )
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert any(c.startswith("issue create") for c in _calls(rate_limit_env))
    closed = [c for c in _calls(rate_limit_env) if c.startswith("issue close")]
    assert not closed, f"reconciled against issues the preflight never filed: {closed}"
    probes = [
        c
        for c in _calls(rate_limit_env)
        if c.startswith("api repos/owner/repo/issues/")
    ]
    assert probes, "the reconciler never probed"


def test_rate_limit_reconciler_stands_down_to_a_racing_sibling(
    rate_limit_env: dict[str, str],
) -> None:
    """A sibling that filed first keeps the record; this leg closes its own.

    The pair only exists because both legs read the list as empty inside the
    window it takes to reflect a fresh create, so the reconcile cannot re-read
    that list — it probes the numbers below its own, which are primary-key
    reads and return the sibling the instant it exists.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    Path(rate_limit_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps([_probe_issue(41, title=PAUSE_TITLE, label=PAUSE_LABEL)])
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert any(c.startswith("issue close 42") for c in calls), (
        f"both legs kept their own record: {calls}"
    )
    # The `::error::` has to name the survivor, not the issue just closed.
    assert "#41" in result.stdout, result.stdout


def test_rate_limit_carries_its_row_onto_the_racing_sibling(
    rate_limit_env: dict[str, str],
) -> None:
    """Standing down must not strand the refused run's row.

    Here the row *is* the notice: the `::error::` sends the maintainer to the
    survivor, and closing that issue is what lifts the ceiling. So the leg that
    stands down has to move its row across first — otherwise the one artifact a
    person is asked to act on is the one missing the run it refused.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    Path(rate_limit_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps([_probe_issue(41, title=PAUSE_TITLE, label=PAUSE_LABEL)])
    )
    # A sibling from another workflow: its seed row cites a different run.
    Path(rate_limit_env["KEEPER_JSON"]).write_text(
        json.dumps({"body": "run 999 row", "comments": []})
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert any(c.startswith("issue comment 41") for c in calls), (
        f"closed its own record without carrying the row over: {calls}"
    )
    assert any(c.startswith("issue close 42") for c in calls), calls
    assert RUN_LINK in _comments(rate_limit_env), _comments(rate_limit_env)


def test_rate_limit_relabelled_issue_does_not_carry_its_closes(
    rate_limit_env: dict[str, str],
) -> None:
    """Moving the label onto an already-closed issue grants nothing.

    The bot holds `issues: write`, so it can label any issue. Were approvals
    counted from the whole history, labelling one a maintainer had closed
    earlier today would import that close as an approval nobody gave. Only
    closes after the label went on count, and on a real pause issue the label
    goes on at creation, so nothing genuine is excluded.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(
        rate_limit_env,
        _closed_event("maintainer"),
        labelled_at=f"{TODAY}T10:00:00Z",
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "a close predating the label counted as an approval"


def test_rate_limit_skips_the_issue_when_the_burst_limit_refused(
    rate_limit_env: dict[str, str],
) -> None:
    """A burst trip files nothing: closing the issue could not lift it.

    The burst limit is deliberately not resumable, so an issue offering to
    double the ceiling would promise a recovery it cannot deliver.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    rate_limit_env["FAKE_RECENT_PRS"] = "11"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert not any(c.startswith("issue create") for c in calls), (
        f"filed a rate-limit issue for a burst trip it cannot lift: {calls}"
    )


def test_rate_limit_refuses_to_run_without_an_identity(
    rate_limit_env: dict[str, str],
) -> None:
    """Unable to read its own identity, the preflight stops rather than guesses.

    Every count and the approval filter are keyed on who the bot is. Carrying
    on without that would leave the counts matching nothing and the filter
    matching every close — a check that has silently reversed rather than
    failed.
    """
    rate_limit_env["FAIL_WHOAMI"] = "1"

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    assert "could not read the bot's own identity" in result.stdout


def test_rate_limit_github_app_cannot_approve(rate_limit_env: dict[str, str]) -> None:
    """A close by an App — `github-actions[bot]` — is not an approval either.

    It is not the bot account, so the login check alone would let a workflow
    holding `GITHUB_TOKEN` wave the limit through.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("github-actions[bot]", actor_type="Bot"))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "a GitHub App counted as an approving human"


def test_rate_limit_yesterdays_approval_does_not_carry(
    rate_limit_env: dict[str, str],
) -> None:
    """Approvals are scoped to today, since the count they lift resets daily.

    The label is dated a day back too, so the day floor is what excludes this
    close. Left at today's default, the label-ordering rule would exclude it
    first and this test would pass without the floor.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(
        rate_limit_env,
        _closed_event("maintainer", day="2026-01-01"),
        labelled_at="2026-01-01T08:00:00Z",
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "yesterday's approval lifted today's ceiling"


def test_rate_limit_foreign_issue_is_not_the_anchor(
    rate_limit_env: dict[str, str],
) -> None:
    """Only an issue the preflight filed anchors the approval.

    The bot holds `issues: write`, so it can label anything. Were the label the
    whole predicate, the lowest-numbered issue carrying it would be nominated
    and a close on it read as an approval nobody gave. The title half runs
    through the script's real `--jq`; the author half is a server-side flag the
    fake can't apply, so it is asserted on the call the script made.
    """
    rate_limit_env["FAKE_TODAY_POSTS"] = "16"
    _approve(rate_limit_env, _closed_event("maintainer"))
    Path(rate_limit_env["PAUSE_ISSUES_JSON"]).write_text(
        json.dumps([{"number": 7, "title": "Something a maintainer labelled"}])
    )

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1, "a foreign issue was taken as the anchor"
    # `--state all` is the anchor lookup; the reconciler's own list is
    # `--state open`, and would otherwise satisfy this on its own.
    lookups = [
        c
        for c in _calls(rate_limit_env)
        if c.startswith("issue list") and "--state all" in c
    ]
    assert lookups, "the anchor lookup never ran"
    assert all("--author @me" in c for c in lookups), (
        f"the anchor lookup is not scoped to issues the bot authored: {lookups}"
    )


def test_rate_limit_reopens_rather_than_refiling(
    rate_limit_env: dict[str, str],
) -> None:
    """Past the doubled ceiling the existing issue is reopened, not duplicated."""
    rate_limit_env["FAKE_TODAY_POSTS"] = "40"
    _approve(rate_limit_env, _closed_event("maintainer"))

    result = _run_preflight(rate_limit_env)

    assert result.returncode == 1
    calls = _calls(rate_limit_env)
    assert any(c.startswith("issue reopen 42") for c in calls), calls
    assert not any(c.startswith("issue create") for c in calls), (
        f"filed a second pause issue instead of reopening #42: {calls}"
    )


# ---------------------------------------------------------------------------
# review-gate.sh — the tend-review pre-check inlined into generated workflows
# ---------------------------------------------------------------------------

REVIEW_GATE = REPO_ROOT / "generator" / "src" / "tend" / "templates" / "review-gate.sh"

FAKE_GH_REVIEW_GATE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$2" in
  repos/*/pulls/*)
    [ -n "${FAIL_PR:-}" ] && exit 1
    cat "$PR_JSON"
    ;;
  repos/*/commits/*/status\\?per_page=100)
    [ -n "${FAIL_STATUS:-}" ] && exit 1
    cat "$STATUS_JSON"
    ;;
  *)
    exit 1
    ;;
esac
"""


@pytest.fixture
def gate_env(tmp_path: Path) -> dict[str, str]:
    """A fake `gh` on PATH plus the workflow env the gate script reads."""
    bindir = fake_bin(tmp_path, gh=FAKE_GH_REVIEW_GATE)

    pr = tmp_path / "pr.json"
    pr.write_text(json.dumps({"state": "open", "head": {"sha": "abc123"}}))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"statuses": []}))

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_REPOSITORY": "owner/repo",
        "PR": "7",
        "EVENT_ACTION": "synchronize",
        "PR_JSON": str(pr),
        "STATUS_JSON": str(status),
    }


def _run_gate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # `bash -e` mirrors the shell GitHub Actions gives a `run:` block.
    return subprocess.run(
        [BASH, "-e", str(REVIEW_GATE)],
        env=env,
        capture_output=True,
        text=True,
    )


def _stamp(env: dict[str, str], *statuses: dict[str, str]) -> None:
    Path(env["STATUS_JSON"]).write_text(json.dumps({"statuses": list(statuses)}))


def test_review_gate_skips_a_stamped_head(gate_env: dict[str, str]) -> None:
    """A `synchronize` whose live HEAD carries this PR's stamp is a no-op."""
    _stamp(gate_env, {"context": "tend-review/7", "state": "success"})

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "false"


def test_review_gate_runs_when_head_is_unstamped(gate_env: dict[str, str]) -> None:
    """Foreign contexts don't gate: another PR's stamp (one branch can be two
    open PRs with different bases) and a non-success state both leave the
    review to run."""
    _stamp(
        gate_env,
        {"context": "tend-review/8", "state": "success"},
        {"context": "tend-review/7", "state": "pending"},
        {"context": "ci/tests", "state": "success"},
    )

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"


def test_review_gate_only_gates_synchronize(gate_env: dict[str, str]) -> None:
    """`opened`/`reopened`/`ready_for_review` always run — with no API calls,
    so a GitHub blip can't fail the ungated path."""
    gate_env["EVENT_ACTION"] = "ready_for_review"
    _stamp(gate_env, {"context": "tend-review/7", "state": "success"})

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"
    assert not Path(gate_env["GH_CALLS"]).exists(), "ungated event still hit the API"


def test_review_gate_skips_closed_prs(gate_env: dict[str, str]) -> None:
    """A queued run whose PR was merged or closed while it waited is a no-op."""
    Path(gate_env["PR_JSON"]).write_text(
        json.dumps({"state": "closed", "head": {"sha": "abc123"}})
    )

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "false"


@pytest.mark.parametrize("failure", ["FAIL_PR", "FAIL_STATUS"])
def test_review_gate_fails_open_on_api_errors(
    gate_env: dict[str, str], failure: str
) -> None:
    """An API error must boot the agent, not silently skip the review."""
    _stamp(gate_env, {"context": "tend-review/7", "state": "success"})
    gate_env[failure] = "1"

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"


def test_review_gate_fails_open_on_an_html_200(gate_env: dict[str, str]) -> None:
    """A GitHub blip can return an HTML error page with a 200: `gh` exits zero
    but the body isn't JSON. The parse must stay inside the fail-open guard —
    unguarded under the run block's `bash -e` it fails the step, skipping the
    whole review (fail-closed)."""
    _stamp(gate_env, {"context": "tend-review/7", "state": "success"})
    Path(gate_env["PR_JSON"]).write_text("<html>oops</html>")

    result = _run_gate(gate_env)

    assert result.returncode == 0, result.stderr
    assert _output(gate_env, "should_run") == "true"


# ---------------------------------------------------------------------------
# notifications-check.sh — the tend-notifications pre-check
# ---------------------------------------------------------------------------

NOTIFICATIONS_CHECK = (
    REPO_ROOT / "generator" / "src" / "tend" / "templates" / "notifications-check.sh"
)

# `gh` stand-in for the notifications pre-check. Fixtures in, the script's own
# `--jq` doing the filtering — the tend-workflow name regex and the PR
# author/state read are the behaviour under test, so a pre-filtered fake would
# assert nothing.
FAKE_GH_NOTIFICATIONS = (
    GH_PREAMBLE
    + r"""
case "$2" in
  notifications)
    # The script's diagnostic re-fetch on a failed attempt. The real one exits
    # non-zero on an error status, which is what its `|| true` tolerates.
    [ "$3" = "-i" ] && { echo "HTTP/2.0 502 Bad Gateway"; exit 1; }
    # Fail the fetches in [FROM, UNTIL], so each consumer of the fetch can be
    # failed on its own: FROM=1 fails every attempt, FROM=1 UNTIL=1 leaves the
    # retry to succeed, and FROM=2 fails only the Layer-D recount.
    if [ -n "${FAIL_NOTIFS_FROM:-}" ]; then
      n=$(( $(cat "$FETCH_CALLS" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$FETCH_CALLS"
      if [ "$n" -ge "$FAIL_NOTIFS_FROM" ] \
        && { [ -z "${FAIL_NOTIFS_UNTIL:-}" ] || [ "$n" -le "$FAIL_NOTIFS_UNTIL" ]; }; then
        exit 1
      fi
    fi
    # A 200 carrying something other than JSON, verbatim.
    if [ -n "${RAW_BODY:-}" ]; then cat "$RAW_BODY"; exit 0; fi
    # A thread marked read leaves the unread listing, so a later fetch must not
    # return it — which is what the Layer-D recount exists to observe.
    jq -c --rawfile done "$READ_THREADS" \
      '($done | split("\n")) as $d | [.[] | select(.id | IN($d[]) | not)]' \
      "$NOTIFICATIONS_JSON"
    ;;
  notifications/threads/*)
    echo "${2##*/}" >> "$READ_THREADS"
    ;;
  repos/*/actions/runs*) emit "$(cat "$RUNS_JSON")" ;;
  repos/*/pulls/*)
    # 404 for a PR the fixture doesn't carry, which the script's `|| continue`
    # has to survive under `bash -e`.
    pr=$(jq -c --argjson n "${2##*/}" \
      'map(select(.number == $n)) | .[0] // empty' "$PULLS_JSON")
    [ -n "$pr" ] || exit 1
    emit "$pr"
    ;;
  *) exit 1 ;;
esac
"""
)

# The fake `date` puts "now" at 12:00, so Layer D's 10-minute deferral window
# opens at 11:50 and Layer B's shadowed-run lookback at 11:30.
NOTIF_FRESH = "2026-01-02T11:55:00Z"
NOTIF_SETTLED = "2026-01-02T11:45:00Z"


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
    """Fake gh/date/sleep on PATH, plus the workflow env the pre-check reads."""
    bindir = fake_bin(
        tmp_path, gh=FAKE_GH_NOTIFICATIONS, date=FAKE_DATE, sleep=FAKE_SLEEP
    )

    notifications = tmp_path / "notifications.json"
    notifications.write_text("[]")
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({"workflow_runs": []}))
    pulls = tmp_path / "pulls.json"
    pulls.write_text("[]")
    read_threads = tmp_path / "read-threads"
    read_threads.write_text("")

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "FETCH_CALLS": str(tmp_path / "fetch-calls"),
        "READ_THREADS": str(read_threads),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_REPOSITORY": "owner/repo",
        "BOT_NAME": "tend-agent",
        "NOTIFICATIONS_JSON": str(notifications),
        "RUNS_JSON": str(runs),
        "PULLS_JSON": str(pulls),
    }


def _run_check(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # `bash -e` mirrors the shell GitHub Actions gives a `run:` block.
    return subprocess.run(
        [BASH, "-e", str(NOTIFICATIONS_CHECK)],
        env=env,
        capture_output=True,
        text=True,
    )


def _marked_read(env: dict[str, str]) -> list[str]:
    return Path(env["READ_THREADS"]).read_text().split()


def _write_json(env: dict[str, str], key: str, value: object) -> None:
    Path(env[key]).write_text(json.dumps(value))


def test_notifications_check_reports_no_work_on_an_empty_inbox(
    notifications_env: dict[str, str],
) -> None:
    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "0"
    assert _marked_read(notifications_env) == []


@pytest.mark.parametrize(
    ("updated_at", "repo", "expected"),
    [
        # A dedicated workflow (review/mention/triage/ci-fix) is likely still
        # mid-flight on this one, so processing it now would duplicate its work.
        (NOTIF_FRESH, "owner/repo", "0"),
        (NOTIF_SETTLED, "owner/repo", "1"),
        # No dedicated workflow covers another repo, so there is nothing to wait
        # for however fresh the notification is.
        (NOTIF_FRESH, "other/repo", "1"),
    ],
)
def test_notifications_check_defers_only_fresh_same_repo_work(
    notifications_env: dict[str, str], updated_at: str, repo: str, expected: str
) -> None:
    _write_json(
        notifications_env,
        "NOTIFICATIONS_JSON",
        [_notif("999", "issues", 7, updated_at, repo=repo)],
    )

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == expected


def test_notifications_check_clears_what_a_recent_tend_run_covered(
    notifications_env: dict[str, str],
) -> None:
    """A dedicated run that failed before its post-step leaves the notification
    unread; clearing it here saves an agent turn rediscovering it. Matched on
    the tend workflow names, so a run of the repo's own CI clears nothing.
    """
    _write_json(
        notifications_env,
        "NOTIFICATIONS_JSON",
        [
            _notif("11", "pulls", 7, NOTIF_SETTLED),
            _notif("22", "pulls", 8, NOTIF_SETTLED),
        ],
    )
    _write_json(
        notifications_env,
        "RUNS_JSON",
        {
            "workflow_runs": [
                {"name": "tend-review", "pull_requests": [{"number": 7}]},
                {"name": "ci", "pull_requests": [{"number": 8}]},
            ]
        },
    )
    # Open, and someone else's, so Layer C leaves both alone.
    _write_json(
        notifications_env,
        "PULLS_JSON",
        [
            {"number": 7, "user": {"login": "human"}, "state": "open"},
            {"number": 8, "user": {"login": "human"}, "state": "open"},
        ],
    )

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _marked_read(notifications_env) == ["11"]
    assert _output(notifications_env, "count") == "1"


def test_notifications_check_clears_the_bots_own_closed_prs(
    notifications_env: dict[str, str],
) -> None:
    """The bot auto-subscribes to its own PRs, so one that's closed is noise.
    Someone else's closed PR, the bot's still-open PR, and a PR that can't be
    read at all each stay unread and countable.
    """
    _write_json(
        notifications_env,
        "NOTIFICATIONS_JSON",
        [_notif(str(n * 11), "pulls", n, NOTIF_SETTLED) for n in (1, 2, 3, 4)],
    )
    _write_json(
        notifications_env,
        "PULLS_JSON",
        [
            {"number": 1, "user": {"login": "tend-agent"}, "state": "closed"},
            {"number": 2, "user": {"login": "human"}, "state": "closed"},
            {"number": 3, "user": {"login": "tend-agent"}, "state": "open"},
        ],
    )

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _marked_read(notifications_env) == ["11"]
    assert _output(notifications_env, "count") == "3"


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
    notifications_env["FAIL_NOTIFS_FROM"] = "1"

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "0"
    assert _marked_read(notifications_env) == []


def test_notifications_check_retries_a_transient_fetch_failure(
    notifications_env: dict[str, str],
) -> None:
    """One failed attempt costs the cycle nothing: the retry enumerates."""
    _write_json(
        notifications_env,
        "NOTIFICATIONS_JSON",
        [_notif("999", "issues", 7, NOTIF_SETTLED)],
    )
    notifications_env["FAIL_NOTIFS_FROM"] = "1"
    notifications_env["FAIL_NOTIFS_UNTIL"] = "1"

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _output(notifications_env, "count") == "1"


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


def test_notifications_check_counts_from_the_snapshot_when_the_recount_fails(
    notifications_env: dict[str, str],
) -> None:
    """A failed recount over-counts by whatever Layers B/C cleared, spending one
    agent run — the alternative, a zero count, would strand real work until the
    next cycle.
    """
    _write_json(
        notifications_env,
        "NOTIFICATIONS_JSON",
        [
            _notif("11", "pulls", 1, NOTIF_SETTLED),
            _notif("999", "issues", 7, NOTIF_SETTLED),
        ],
    )
    # Layer C clears this one, so a recount that ran would have returned 1 —
    # which is what separates the fallback from a quietly successful recount.
    _write_json(
        notifications_env,
        "PULLS_JSON",
        [{"number": 1, "user": {"login": "tend-agent"}, "state": "closed"}],
    )
    notifications_env["FAIL_NOTIFS_FROM"] = "2"

    result = _run_check(notifications_env)

    assert result.returncode == 0, result.stderr
    assert _marked_read(notifications_env) == ["11"]
    assert _output(notifications_env, "count") == "2"


REPORT_FAILURE = REPO_ROOT / "shared" / "steps" / "report-failure.sh"
RUN_ISSUE_LIB = REPO_ROOT / "shared" / "steps" / "lib" / "run-issue.sh"

OUTAGE_TITLE = "Bot temporarily unavailable"
OUTAGE_LABEL = "tend-outage"

# The created_at the fake stamps on a row as it posts it. Later than every
# seeded comment below, so the reconcile's earliest-wins keeper is the seeded
# one and the row just posted is the duplicate.
POSTED_AT = "2026-01-02T12:00:00Z"

# `gh` stand-in for the outage reporter. Same shape as the rate-limit fake —
# fixtures in, the script's own `--jq` doing the filtering — plus it captures
# comment bodies, which arrive on stdin (`-F -`) rather than in the args.
FAKE_GH_REPORT_FAILURE = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_CALLS"

jq_expr=""
slurp=""
prev=""
for arg in "$@"; do
  [ "$prev" = "--jq" ] && jq_expr="$arg"
  [ "$arg" = "--slurp" ] && slurp=1
  prev="$arg"
done

# Real `gh` refuses the combination outright and exits 1, and the comment
# reconcile's shape rests on that: fold the filter back into `--jq` and the
# script dies under pipefail just after posting its row, never reconciling.
if [ -n "$slurp" ] && [ -n "$jq_expr" ]; then
  echo "the --slurp option is not supported with --jq or --template" >&2
  exit 1
fi

emit() {
  if [ -n "$jq_expr" ]; then
    printf '%s' "$1" | jq -r "$jq_expr"
  else
    printf '%s' "$1"
  fi
}

case "$1 $2" in
  "api user") emit '{"login":"tend-agent","id":4242}' ;;
  "issue list")
    if [ -n "${FAIL_ISSUE_LIST_FROM:-}" ]; then
      n=$(( $(cat "$LIST_CALLS" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$LIST_CALLS"
      if [ "$n" -ge "$FAIL_ISSUE_LIST_FROM" ]; then exit 1; fi
    fi
    emit "$(cat "$OPEN_ISSUES_JSON")"
    ;;
  "issue create") echo "https://github.com/owner/repo/issues/${FAKE_NEW_ISSUE}" ;;
  "issue view") emit "$(cat "$KEEPER_JSON")" ;;
  "issue comment")
    body=$(cat)
    printf '%s\n' "$body" >> "$COMMENT_BODIES"
    # Bare `[ -n ... ] && exit 1` would leave the failing test as the case
    # body's status and so fail every call; keep it an `if`. Fails before the
    # row lands in the comment list: a post that 5xx'd left no comment behind.
    if [ -n "${FAIL_ISSUE_COMMENT:-}" ]; then exit 1; fi
    # Land it in the comment list too, so the reconcile that runs straight
    # after sees the row this call just posted.
    jq -c --arg b "$body" --arg t "$POSTED_AT" \
      '. + [{id: ((map(.id) | max // 0) + 1), created_at: $t, body: $b}]' \
      "$ISSUE_COMMENTS_JSON" > "$ISSUE_COMMENTS_JSON.tmp"
    mv "$ISSUE_COMMENTS_JSON.tmp" "$ISSUE_COMMENTS_JSON"
    ;;
  "issue close" | "label create") ;;
  *)
    # Matched against the whole arg list, not "$2": these calls carry flags
    # (`--paginate`, `-X DELETE`) where the path would otherwise sit.
    case "$*" in
      *"/comments?per_page=100"*)
        if [ -n "${FAIL_COMMENT_LIST:-}" ]; then
          echo "gh: 502 server error" >&2
          exit 1
        fi
        # Paged the way the endpoint pages, whether or not the caller asked
        # for every page: `--slurp` gets the array of pages, a plain read gets
        # the oldest 100 alone. Both go through `emit`, so a caller passing
        # `--jq` has its own filter applied to what it actually received.
        if [ -n "$slurp" ]; then
          # Sliced by index rather than with `_nwise`, which jq 1.8 dropped.
          # An empty list still pages as `[[]]`, the shape real `gh` returns.
          emit "$(jq -c '. as $a | [range(0; ([($a|length),1]|max); 100) | $a[.:.+100]]' \
            "$ISSUE_COMMENTS_JSON")"
        else
          emit "$(jq -c '.[0:100]' "$ISSUE_COMMENTS_JSON")"
        fi
        ;;
      *"-X DELETE"*) ;;
      # The reconciler's primary-key probe. Last, so the two paths above —
      # whose URLs also contain `/issues/` — are matched first.
      *repos/*/issues/*)
        emit "$(jq -c --argjson n "${2##*/}" \
          'map(select(.number == $n)) | .[0] // {"number":0}' "$PROBE_ISSUES_JSON")"
        ;;
      *) exit 1 ;;
    esac
    ;;
esac
"""


@pytest.fixture
def report_failure_env(tmp_path: Path) -> dict[str, str]:
    """Fake gh/sleep on PATH, plus the Actions env the reporter reads."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in (("gh", FAKE_GH_REPORT_FAILURE), ("sleep", FAKE_SLEEP)):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)

    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 851}}))
    for name in ("open-issues.json", "probe-issues.json", "issue-comments.json"):
        (tmp_path / name).write_text("[]")
    (tmp_path / "keeper.json").write_text('{"body": "", "comments": []}')
    (tmp_path / "comment-bodies.txt").write_text("")

    return {
        "PATH": tool_path(bindir),
        "GH_CALLS": str(tmp_path / "gh-calls.log"),
        "LIST_CALLS": str(tmp_path / "list-calls"),
        "OPEN_ISSUES_JSON": str(tmp_path / "open-issues.json"),
        "PROBE_ISSUES_JSON": str(tmp_path / "probe-issues.json"),
        "ISSUE_COMMENTS_JSON": str(tmp_path / "issue-comments.json"),
        "KEEPER_JSON": str(tmp_path / "keeper.json"),
        "COMMENT_BODIES": str(tmp_path / "comment-bodies.txt"),
        "POSTED_AT": POSTED_AT,
        "FAKE_NEW_ISSUE": "42",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_EVENT_PATH": str(event),
    }


def _run_report_failure(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(REPORT_FAILURE)], env=env, capture_output=True, text=True
    )


def _outage_probe(number: int, **kw) -> dict:
    kw.setdefault("title", OUTAGE_TITLE)
    kw.setdefault("label", OUTAGE_LABEL)
    return _probe_issue(number, **kw)


def test_report_failure_files_when_nothing_is_open(
    report_failure_env: dict[str, str],
) -> None:
    """No open tracker and no racing sibling: file one and keep it."""
    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert any(c.startswith("issue create") for c in calls), calls
    assert not any(c.startswith("issue close") for c in calls), (
        f"closed the tracker it had just filed: {calls}"
    )


def test_report_failure_appends_to_the_open_tracker(
    report_failure_env: dict[str, str],
) -> None:
    """An open tracker takes the row as a comment rather than a second issue."""
    Path(report_failure_env["OPEN_ISSUES_JSON"]).write_text(
        json.dumps([{"number": 8, "title": OUTAGE_TITLE}])
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert any(c.startswith("issue comment 8") for c in calls), calls
    assert not any(c.startswith("issue create") for c in calls), (
        f"filed a second tracker while one was open: {calls}"
    )


def test_report_failure_files_nothing_when_the_issue_list_cannot_be_read(
    report_failure_env: dict[str, str],
) -> None:
    """The same conflation, from the other caller.

    Two open trackers is the state that breaks the drain sweep: later rows
    scatter across both and neither carries the complete set. The reconcile's
    downward probe does not reach an older tracker, so the duplicate persists.
    Skipping costs this one row, and the next failure records normally.
    """
    Path(report_failure_env["OPEN_ISSUES_JSON"]).write_text(
        json.dumps([{"number": 8, "title": OUTAGE_TITLE}])
    )
    report_failure_env["FAIL_ISSUE_LIST_FROM"] = "1"

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert not any(c.startswith("issue create") for c in calls), calls
    assert "::warning::" in result.stdout


def test_report_failure_survives_a_failed_append_to_the_open_tracker(
    report_failure_env: dict[str, str],
) -> None:
    """A 5xx on the append must not abort the step.

    This is the common write path — once a tracker is open, every later
    failure in the same incident appends through it. Left bare under `set -e`
    the abort costs a second red step on an already-failing run and drops the
    row silently, so the tracker under-reports the outage and a run stranded
    by it reads as one that never happened.

    The paired `..._propagates_a_failed_create` below asserts the opposite for
    the other branch, and the asymmetry is the point: an append has a tracker
    already carrying the incident, a create has nothing to fall back to.
    """
    Path(report_failure_env["OPEN_ISSUES_JSON"]).write_text(
        json.dumps([{"number": 8, "title": OUTAGE_TITLE}])
    )
    report_failure_env["FAIL_ISSUE_COMMENT"] = "1"

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stdout, result.stdout
    calls = _calls(report_failure_env)
    assert not any(c.startswith("issue create") for c in calls), (
        f"filed a second tracker after the append failed: {calls}"
    )


def test_report_failure_carries_its_row_onto_the_racing_sibling(
    report_failure_env: dict[str, str],
) -> None:
    """Standing down must not strand the failure it recorded.

    The row lives in the body of the issue this leg filed, so closing that
    issue takes the row with it unless it is carried onto the survivor first.

    Two siblings rather than one, because with a single match "lowest" and
    "nearest" are the same answer and the choice between them goes untested.
    Convergence rests on lowest: a third leg filing #43 sees both #41 and #38,
    and only if every leg keeps descending past the first hit do they agree on
    one keeper instead of scattering rows across two.
    """
    Path(report_failure_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps([_outage_probe(41), _outage_probe(38)])
    )
    # A sibling from another workflow: its seed row cites a different run.
    Path(report_failure_env["KEEPER_JSON"]).write_text(
        json.dumps({"body": "run 999 row", "comments": []})
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert any(c.startswith("issue comment 38") for c in calls), (
        f"carried the row onto the nearest sibling rather than the lowest: {calls}"
    )
    assert not any(c.startswith("issue comment 41") for c in calls), (
        f"stopped at the first hit instead of descending to the lowest: {calls}"
    )
    assert any(c.startswith("issue close 42") for c in calls), calls
    assert "Duplicate of #38" in " ".join(calls), calls
    assert RUN_LINK in _comments(report_failure_env), _comments(report_failure_env)


def test_report_failure_does_not_repeat_a_row_the_keeper_already_has(
    report_failure_env: dict[str, str],
) -> None:
    """Matrix legs share one run id, so the keeper's seed row is already ours."""
    Path(report_failure_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps([_outage_probe(41)])
    )
    Path(report_failure_env["KEEPER_JSON"]).write_text(
        json.dumps({"body": f"| when | {RUN_LINK} | #851 |", "comments": []})
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert any(c.startswith("issue close 42") for c in calls), calls
    assert not any(c.startswith("issue comment") for c in calls), (
        f"repeated a row the keeper already carried: {calls}"
    )


def test_report_failure_does_not_adopt_a_foreign_issue(
    report_failure_env: dict[str, str],
) -> None:
    """The bot holds `issues: write`, so the label alone nominates nothing."""
    Path(report_failure_env["PROBE_ISSUES_JSON"]).write_text(
        json.dumps(
            [
                _outage_probe(41, login="someone"),
                _outage_probe(40, title="A maintainer's issue"),
                _outage_probe(39, label="unrelated-label"),
                _outage_probe(38, state="closed"),
            ]
        )
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    calls = _calls(report_failure_env)
    assert not any(c.startswith("issue close") for c in calls), (
        f"stood down to an issue the reporter never filed: {calls}"
    )


def test_report_failure_propagates_a_failed_create(
    report_failure_env: dict[str, str], tmp_path: Path
) -> None:
    """A create that fails must redden the step, not report a phantom issue.

    Under `set -e` the create has to stay in its own assignment: wrapped in
    another command its status would be the wrapper's, and a failed create
    would sail past with an empty issue number and the outage unrecorded.
    """
    gh = Path(report_failure_env["PATH"].split(":")[0]) / "gh"
    gh.write_text(
        FAKE_GH_REPORT_FAILURE.replace(
            '"issue create") echo "https://github.com/owner/repo/issues/${FAKE_NEW_ISSUE}" ;;',
            '"issue create") echo "gh: API error" >&2; exit 1 ;;',
        )
    )
    gh.chmod(0o755)

    result = _run_report_failure(report_failure_env)

    assert result.returncode != 0, (
        f"a failed create left the step green; stdout:\n{result.stdout}"
    )


def test_run_issue_reconcile_refuses_a_call_with_no_row(
    report_failure_env: dict[str, str],
) -> None:
    """Both callers pass a row, so omitting one is a bug, not a mode.

    It has to abort *before* the create: a leg that files an issue and then
    stands down without carrying its row over strands the incident in the
    duplicate it closes, which is the failure the carry-over exists to prevent.
    """
    result = subprocess.run(
        [
            BASH,
            "-c",
            f'. "{RUN_ISSUE_LIB}"'
            f"; run_issue_create_and_reconcile {OUTAGE_LABEL} {OUTAGE_TITLE!r}"
            "; echo REACHED",
        ],
        env=report_failure_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, result.stdout
    assert "REACHED" not in result.stdout, (
        f"ran on past a call with no row: {result.stdout}"
    )
    assert "the row for this run is required" in result.stderr, result.stderr
    calls = Path(report_failure_env["GH_CALLS"])
    assert not calls.exists(), f"reached gh before refusing: {calls.read_text()}"


# ---------------------------------------------------------------------------
# report-failure.sh — the append path, and the per-run comment dedup
# ---------------------------------------------------------------------------


def _deleted(env: dict[str, str]) -> list[str]:
    """Comment ids the reconcile deleted."""
    return [c.rsplit("/", 1)[-1] for c in _calls(env) if "-X DELETE" in c]


def _issue_comments(env: dict[str, str], *comments: dict) -> None:
    """Seed the comment list the reconcile reads, oldest-first as the API serves it."""
    Path(env["ISSUE_COMMENTS_JSON"]).write_text(json.dumps(list(comments)))


def _comment(number: int, body: str, at: str) -> dict:
    return {"id": number, "created_at": at, "body": body}


def _filler(count: int, *, first_id: int = 1) -> list[dict]:
    """Unrelated comments, none carrying this run's anchor."""
    return [
        _comment(first_id + i, f"nightly enrichment {i}", f"2026-01-01T00:{i:02d}:00Z")
        for i in range(count)
    ]


def _seen_by_the_guard(env: dict[str, str], *bodies: str, body: str = "") -> None:
    """What `gh issue view --json body,comments` returns for the tracker."""
    Path(env["KEEPER_JSON"]).write_text(
        json.dumps({"body": body, "comments": [{"body": b} for b in bodies]})
    )


def _open_tracker(env: dict[str, str], number: int = 42) -> None:
    """An outage tracker already open, so the reporter takes the append path.

    Set per-test rather than in the fixture: the create-path tests above start
    from an empty list, and these five need the opposite.
    """
    Path(env["OPEN_ISSUES_JSON"]).write_text(
        json.dumps([{"number": number, "title": OUTAGE_TITLE}])
    )


@pytest.mark.parametrize(
    ("body", "comments"),
    [
        pytest.param("", (f"| when | {RUN_LINK} | #851 |",), id="in-a-comment"),
        pytest.param(f"| when | {RUN_LINK} | #851 |", (), id="in-the-issue-body"),
    ],
)
def test_report_failure_skips_a_run_already_recorded(
    report_failure_env: dict[str, str], body: str, comments: tuple[str, ...]
) -> None:
    """A leg whose sibling already recorded this run posts nothing.

    This is the guard that collapses the flood: a matrix workflow calls the
    script once per leg, every leg sharing one GITHUB_RUN_ID, so without it a
    5-leg matrix leaves 5 comments all citing the same run. The body case is
    the first run of an outage: one leg seeds the issue with its row, and the
    siblings that follow have no comment to match — only the body.
    """
    _open_tracker(report_failure_env)
    _seen_by_the_guard(report_failure_env, *comments, body=body)

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    assert not _comments(report_failure_env), (
        f"appended a second row for a run already recorded: "
        f"{_comments(report_failure_env)!r}"
    )


def test_report_failure_appends_a_row_for_an_unrecorded_run(
    report_failure_env: dict[str, str],
) -> None:
    """The happy path: a run the tracker has not seen still gets its row."""
    _open_tracker(report_failure_env)
    _seen_by_the_guard(report_failure_env, "some other run's row")

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    assert RUN_LINK in _comments(report_failure_env)


def test_report_failure_reconciles_a_racing_leg(
    report_failure_env: dict[str, str],
) -> None:
    """Two legs that both read the tracker before either posted converge to one row.

    The guard is a check-then-act, so jittered legs can both miss. Every leg
    sorts the same list the same way, so each computes the same keeper — the
    earliest — and deletes the rest.
    """
    _open_tracker(report_failure_env)
    _seen_by_the_guard(report_failure_env, "nothing recorded yet")
    _issue_comments(
        report_failure_env,
        _comment(1, f"| when | {RUN_LINK} | #851 |", "2026-01-02T11:59:00Z"),
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    assert _deleted(report_failure_env) == ["2"], (
        f"expected the later of the two rows deleted, got "
        f"{_deleted(report_failure_env)}"
    )


def test_report_failure_reconciles_past_the_first_page(
    report_failure_env: dict[str, str],
) -> None:
    """The flood the reconcile exists for is exactly where it must paginate.

    Issue comments come back oldest-first, so on a tracker past 100 comments an
    unpaginated read returns only the oldest page — the rows this run and its
    racing sibling just posted are not in it, and the reconcile no-ops on the
    one issue that needed it.
    """
    _open_tracker(report_failure_env)
    _seen_by_the_guard(report_failure_env, "nothing recorded yet")
    _issue_comments(
        report_failure_env,
        *_filler(138),
        _comment(139, f"| when | {RUN_LINK} | #851 |", "2026-01-02T11:59:00Z"),
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    assert _deleted(report_failure_env) == ["140"], (
        f"the reconcile did not reach past the first page of comments; deleted "
        f"{_deleted(report_failure_env)}"
    )


def test_report_failure_survives_a_failed_reconcile_read(
    report_failure_env: dict[str, str],
) -> None:
    """A 5xx on the reconcile's read must not redden a step whose row landed.

    The reconcile is best-effort cleanup and the last statement in the append
    branch, so left bare under `set -eo pipefail` a failed read takes the whole
    script's status with it — *after* the write succeeded. That reddens the
    `Report failure` step with no annotation naming why, on precisely the job
    someone is about to diagnose. Duplicate rows on the tracker are the better
    failure: the append immediately above warns and continues for the same
    reason.
    """
    _open_tracker(report_failure_env)
    _seen_by_the_guard(report_failure_env, "nothing recorded yet")
    report_failure_env["FAIL_COMMENT_LIST"] = "1"

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, (
        f"a failed reconcile read reddened the step; stdout:\n{result.stdout}"
    )
    assert "::warning::" in result.stdout, result.stdout
    assert RUN_LINK in _comments(report_failure_env), (
        f"lost the row the reconcile was cleaning up after: "
        f"{_comments(report_failure_env)!r}"
    )


def test_report_failure_leaves_a_human_comment_naming_the_run(
    report_failure_env: dict[str, str],
) -> None:
    """Only the bot's own generated rows are eligible for deletion.

    The reconcile deletes, so its predicate is the whole protection. Selecting
    on the bare run URL would make a person linking the run in discussion — the
    normal way an outage gets diagnosed — a duplicate to be removed.
    """
    _open_tracker(report_failure_env)
    _seen_by_the_guard(report_failure_env, "nothing recorded yet")
    _issue_comments(
        report_failure_env,
        _comment(
            1,
            "https://github.com/owner/repo/actions/runs/12345 is the one that failed",
            "2026-01-02T11:00:00Z",
        ),
    )

    result = _run_report_failure(report_failure_env)

    assert result.returncode == 0, result.stderr
    assert not _deleted(report_failure_env), (
        f"deleted a human comment that merely named the run: "
        f"{_deleted(report_failure_env)}"
    )
