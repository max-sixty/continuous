"""Pin the safety contracts a skill shares with a script or another skill.

A rule split across two files drifts silently: nothing runs both halves
together, so each reads correct on its own while the pair stops agreeing.
"""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from tend.cli import main

from tests import _yaml as yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(*parts: str) -> str:
    return REPO_ROOT.joinpath(*parts).read_text()


def test_notification_skill_uses_one_paginated_cutoff_snapshot() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert "notifications?before=$CUTOFF&per_page=100" in skill
    assert "--paginate --slurp" in skill
    assert "sort_by(.updated_at)" in skill


def test_notification_skill_acknowledges_only_the_threads_it_resolved() -> None:
    """The acknowledgement is per thread, never repository-wide.

    `PUT /repos/{owner}/{repo}/notifications` marks by timestamp rather than by
    outcome, so it acts on threads the run never examined — including a thread
    deferred because its dedicated workflow is still in flight. REST has no
    "mark unread", so that overshoot is unrecoverable. Acknowledging exactly the
    threads that reached an outcome needs no timestamp reasoning at all.
    """
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert 'gh api "notifications/threads/$THREAD_ID" -X PATCH' in skill
    assert "repos/$GITHUB_REPOSITORY/notifications" not in skill
    assert "-f last_read_at=" not in skill
    assert "ACK_CUTOFF" not in skill
    assert "Never acknowledge a thread before it has an outcome" in skill


def test_notification_skill_pins_the_fragile_dedup_queries() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert ".display_title == $title" in skill
    assert "issues/$NUMBER/timeline?per_page=100" in skill
    assert '.event == "cross-referenced"' in skill


def test_frequent_poll_and_nightly_share_conflict_resolution() -> None:
    notifications = _read(
        "plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md"
    )
    nightly = _read("plugins", "tend-ci-runner", "skills", "nightly", "SKILL.md")
    resolver = _read(
        "plugins", "tend-ci-runner", "skills", "resolve-conflicts", "SKILL.md"
    )
    check = _read("generator", "src", "tend", "templates", "notifications_check.py")

    for caller in (notifications, nightly):
        assert "/tend-ci-runner:resolve-conflicts" in caller
    assert "configured bot only" in " ".join(notifications.split())
    assert "this bot and upstream dependency bots" in " ".join(nightly.split())
    assert "app/dependabot" in resolver
    assert "app/renovate" in resolver
    assert "baseRefName" in resolver
    assert "baseRefOid" in resolver
    assert "headRefName" in resolver
    assert "headRepository" in resolver
    assert '"refs/heads/<base>:refs/tend/base/<number>"' in resolver
    assert '"refs/tend/base/<number>" "refs/tend/pr/<number>"' in resolver
    assert "headRefOid" in resolver
    assert '--force-with-lease="refs/heads/<headRefName>:<headRefOid>"' in resolver
    assert "<!-- tend-conflict-deferred head=<head SHA> -->" in resolver
    assert "<!-- tend-conflict-deferred head={pr.get('headRefOid', '')} -->" in check
    assert "comments(last: 100)" in check
    assert '.rstrip().split("\\n")[-1] == marker' in check
    assert "origin/main" not in resolver


def test_review_runs_pins_current_state_recovery() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "review-runs", "SKILL.md")

    assert "--state open --label tend-outage --author @me" in skill
    assert "| sort | .[0] // empty" in skill
    assert "if ! gh issue list" in skill
    assert "> /tmp/review-runs-outage-number; then" in skill
    assert "--json body,comments --jq '.body, .comments[].body'" in skill
    close_block = (
        "OUTAGE=$(cat /tmp/review-runs-outage-number)\n"
        '[ -n "$OUTAGE" ] && gh issue close "$OUTAGE" --reason completed'
    )
    assert close_block in skill
    assert "complete **Reconcile live work** below, then exit" in skill
    assert "Do not replay historical workflow runs" in skill
    assert "an open issue with no bot response to the latest human activity" in skill
    assert "whose live head has no finalized bot review" in skill
    assert "failing default-branch CI with no bot fix in progress" in skill


def test_outage_tracker_title_stays_in_sync() -> None:
    title = "Bot temporarily unavailable"
    reporter = _read("shared", "steps", "report_failure.py")
    review_runs = _read(
        "plugins", "tend-ci-runner", "skills", "review-runs", "SKILL.md"
    )
    ci_fix = _read("plugins", "tend-ci-runner", "skills", "ci-fix", "SKILL.md")

    for content in (reporter, review_runs, ci_fix):
        assert title in content


def test_unreadable_notification_subjects_are_terminal() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert "whose `subject.url` is null" in skill
    assert "whose `subject.url` 404s" in skill
    assert "A read that fails for any other reason" in skill


def test_installation_and_each_poll_enable_repository_watching() -> None:
    install = _read("plugins", "install-tend", "skills", "install-tend", "SKILL.md")
    precheck = _read("generator", "src", "tend", "templates", "notifications_check.py")

    for content in (install, precheck):
        assert "subscription" in content
        assert "subscribed=true" in content
        assert "ignored=false" in content


def test_review_skill_retargets_a_moved_head_rather_than_discarding_it() -> None:
    """A push mid-review re-targets the review rather than throwing it away.

    Re-targeting requires the live head to build on the reviewed one, and every
    review pins the commit it read: unpinned, GitHub anchors it at whatever is
    live when the POST lands, so the review claims code the session never saw.

    The sha reaches the POST through a private pin because the agent composes
    the body between reading the head and posting.
    """
    skill = _read("plugins", "tend-ci-runner", "skills", "review", "SKILL.md")
    preflight = _read("plugins", "tend-ci-runner", "scripts", "review_preflight.py")

    assert "HEAD moved — leaving" not in skill

    # Written by the initial snapshot and rewritten where the head moves.
    assert '"start"' in preflight
    assert (
        'Path(os.environ.get("REVIEWED_HEAD_FILE", "/tmp/reviewed-head"))' in preflight
    )
    assert '"REVIEWED_HEAD_FILE", "/tmp/reviewed-head"' in preflight
    # The script, rather than prose recipes, owns the final state check, pin,
    # metadata, and API request. Arbitrary commands cannot bypass that contract.
    assert '"commit_id": reviewed' in preflight
    assert "review_preflight.py submit" in skill
    assert skill.count("submit <number>") == 3
    assert "post <number> --" not in skill
    direct_launches = re.findall(
        r"/usr/bin/python3 -E -s\s+(?:\\\n\s*)?"
        r'"\$\{CLAUDE_PLUGIN_ROOT\}/scripts/review_preflight\.py"',
        skill,
    )
    assert len(direct_launches) == 6
    assert (
        'uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/review_preflight.py"'
        not in skill
    )
    assert "--edit-review <id>" in skill
    assert "recover: incomplete review <id>" in skill
    assert "shell variables do not\nsurvive between agent tool calls" in skill

    # Both logs reach the session in one stream, so the skill names both halves.
    assert "**Read both halves of the delta file as a pair.**" in skill
    # `--cc` is the only place a conflicted merge's resolution appears: the
    # author commits it inside the merge, where neither log reaches it. It is
    # not a substitute for re-verifying, though — a resolution taking the base
    # side prints no hunks, and a clean merge that only shifts lines prints
    # nothing anywhere, so the override below has to be unconditional.
    assert "git show --cc" in skill
    assert "even if the scoped log printed nothing" in skill
    assert "Re-compose every `suggestion` block after re-targeting" in skill


def test_weekly_approval_pins_the_commit_it_checked() -> None:
    """Weekly approves dependency PRs, the population `nightly` rewrites on
    purpose, so an unpinned approval lands on a commit nothing checked. It
    carries the sha the same way review does, and for the same reason: the
    body is composed with the Write tool in between."""
    weekly = _read("plugins", "tend-ci-runner", "skills", "weekly", "SKILL.md")
    state_script = _read("plugins", "tend-ci-runner", "scripts", "bot_review_state.py")

    # Per-PR and cleared up front: step 2 loops over every dependency PR, so a
    # shared name hands the next PR this one's sha, and the already-approved
    # branch must not leave a readable file behind.
    assert "prepare-approval <number>" in weekly
    assert "pin.unlink(missing_ok=True)" in state_script
    assert 'pin.write_text(f"{head_sha}\\n")' in state_script
    assert "CHECKED=$(cat /tmp/checked-head-<number>) || exit 0" in weekly
    assert '-f commit_id="$CHECKED"' in weekly

    # `gh pr review --approve` cannot pin a commit; both skills post through
    # the reviews endpoint instead.
    skill = _read("plugins", "tend-ci-runner", "skills", "review", "SKILL.md")
    for content in (skill, weekly):
        assert "gh pr review --approve" not in content


def test_review_approval_gates_on_author_stated_readiness() -> None:
    """A PR whose author says it must not merge withholds the verdict the same
    way the draft flag does, and the draft flag is the only signal the skill
    used to read. Every approving path — step 6's no-issues approve, the
    trivial-incremental "your findings are now addressed" approve, and the
    dedup rule's "resolves the last open one" approve — has to reach the gate,
    so each carries a pointer to it.
    """
    skill = _read("plugins", "tend-ci-runner", "skills", "review", "SKILL.md")

    # Stated once, under step 6, where every approving path is sent for the
    # POST recipe.
    assert "**Unless the author withheld merge readiness.**" in skill
    # The bot's own findings closing out is what fired the wrong approval:
    # the two conditions are independent and only the author clears the second.
    assert "independent conditions" in skill

    # The incremental paths approve without reading step 6's prose, so the
    # pointer rides on each sentence that prescribes the approval: the
    # trivial-skip one and the dedup rule's, which fire from the same trigger.
    assert "so the PR isn't left in limbo — and the author-readiness gate" in skill
    assert (
        "resolves the last open one (then approve with an empty body — the "
        "author-readiness gate" in skill
    )

    # Naming the blocker on every push would be the noise the thread-keyed
    # dedup rules suppress for findings but cannot reach for a body-only
    # COMMENT, so the gate carries its own once-only clause.
    assert "name it once" in skill

    # A blocker can also arrive mid-session, after the review began, so the
    # pre-APPROVE peek re-checks it alongside the red-check gate.
    assert "Re-check the author-readiness gate" in skill


def test_review_second_pass_is_a_submit_precondition() -> None:
    """A full review cannot quietly skip the standalone second pass, including
    after a safe re-target; both step-1 close-out paths remain exempt."""
    skill = _read("plugins", "tend-ci-runner", "skills", "review", "SKILL.md")

    second_pass = skill.index("### 5. Second pass")
    submit = skill.index("### 6. Submit")
    assert second_pass < submit
    assert "For a review that reached step 5, before submitting" in skill
    assert "Step 1's trivial-increment and dedup close-out paths" in skill
    assert "Run step 5 again over the updated merged tree" in skill


def test_incomplete_reviews_have_a_survivor_and_a_daily_backstop() -> None:
    review = _read("plugins", "tend-ci-runner", "skills", "review", "SKILL.md")
    review_runs = _read(
        "plugins", "tend-ci-runner", "skills", "review-runs", "SKILL.md"
    )

    assert "`recovery_review_id`" in review
    assert "`incomplete_inline_comments`" in review
    assert "bypass the already-reviewed and\ntrivial-increment silent exits" in review
    assert "do not finish silently" in review
    assert "daily `review-runs` live-work reconciliation" in review
    assert "no finalized bot review" in review_runs
    assert "`tend:review-incomplete` record is not finalized" in review_runs


def test_review_reviewers_matrix_covers_consumers() -> None:
    workflow = yaml.safe_load(_read(".github", "workflows", "review-reviewers.yaml"))
    matrix = workflow["jobs"]["review-reviewers"]["strategy"]["matrix"]["repo"]
    consumers = [entry["repo"] for entry in json.loads(_read("data", "consumers.json"))]

    missing = sorted(set(consumers) - set(matrix))
    assert not missing, f"add consumers to review-reviewers.yaml matrix: {missing}"


def _bash_blocks(markdown: str) -> list[str]:
    return [block.split("```", 1)[0] for block in markdown.split("```bash\n")[1:]]


def test_runner_helper_directory_is_python_only() -> None:
    """Substantial runner behavior belongs in tested Python, not shell helpers."""
    scripts = REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts"

    assert not sorted(scripts.glob("*.sh"))


def test_codex_harness_delegates_stateful_phases_to_its_runner() -> None:
    """Keep CLI parsing and cross-step files out of action-inline Bash."""
    action = _read("codex", "action.yaml")

    for command in ("install-plugin", "stage-agents", "run"):
        assert f'runner.py" {command}' in action
    assert "awk" not in action


def test_every_documented_run_listing_selects_a_profile() -> None:
    callers = [
        _read("plugins", "tend-ci-runner", "skills", "review-runs", "SKILL.md"),
        _read("plugins", "tend-ci-runner", "skills", "review-reviewers", "SKILL.md"),
        _read(".claude", "skills", "running-tend", "SKILL.md"),
    ]

    for caller in callers:
        invocations = [
            block for block in _bash_blocks(caller) if "list_recent_runs.py" in block
        ]
        assert invocations
        for invocation in invocations:
            assert re.search(r"\breview-(?:runs|reviewers)\b", invocation)


def test_nightly_regen_pins_its_poll_to_the_commit_it_pushed() -> None:
    """Step 7 must stash the pushed OID before it removes the regen worktree.

    The commit is made on `tend/update-workflows` inside `/tmp`, and the block
    destroys that worktree on the way out. Afterwards the main checkout's
    `git rev-parse HEAD` — the derivation **CI Monitoring** prescribes "after
    your own push" — resolves to the default branch, a different commit on a
    different branch, so the session is steered to the two sources tend has
    ruled out: the PR head (which a sibling push retargets mid-poll) or the
    abbreviated OID retyped out of `git commit`'s output.
    """
    skill = _read("plugins", "tend-ci-runner", "skills", "nightly", "SKILL.md")
    script = _read("plugins", "tend-ci-runner", "scripts", "nightly_workflow_update.py")

    commit = script.index('"git", "commit"')
    capture = script.index('sha_path.write_text(f"{sha}\\n")')
    cleanup = script.index('"git", "worktree", "remove"', capture)
    assert commit < capture < cleanup
    assert 'poll <pr-number> "$(cat /tmp/tend-update-sha)"' in skill


def test_nightly_regen_stages_every_path_init_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 7's `git add -A` pathspecs must cover everything `tend init` writes.

    The recipe names a fixed pathspec while the generator's output set can
    grow, so every generated path must remain covered.
    """
    script = _read("plugins", "tend-ci-runner", "scripts", "nightly_workflow_update.py")
    normalized = re.sub(r"\s+", " ", script)
    staged = [".github", ".config"]
    assert script.count('"git", "add", "-A", ".github", ".config"') == 2

    # The stamp-only skip and the `git status` inspection both read the staged
    # set: a file `init` newly created is invisible to a plain `git diff`, so a
    # release whose only change is a new output path would skip the PR as a
    # no-op. They must name the same paths the `git add -A` lines stage —
    # widening only the staging leaves the no-op check blind to the new path.
    assert '"git", "status", "--porcelain", ".github", ".config"' in normalized
    assert (
        '"git", "diff", "--cached", "--no-color", "--", ".github", ".config"'
        in normalized
    )

    config = tmp_path / ".config" / "tend.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("bot_name: test-bot\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0, result.output

    written = {
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    }
    uncovered = sorted(
        path
        for path in written
        if not any(path == spec or path.startswith(f"{spec}/") for spec in staged)
    )
    assert not uncovered, (
        f"`tend init` writes paths the nightly regeneration never stages: {uncovered}. "
        "Widen Step 7's `git add -A` pathspecs so the regeneration PR carries them."
    )
