"""Pin the safety contracts a skill shares with a script or another skill.

A rule split across two files drifts silently: nothing runs both halves
together, so each reads correct on its own while the pair stops agreeing.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(*parts: str) -> str:
    return REPO_ROOT.joinpath(*parts).read_text()


def test_notification_skill_uses_one_paginated_cutoff_snapshot() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert "notifications?before=$CUTOFF&per_page=100" in skill
    assert "--paginate --slurp" in skill
    assert "sort_by(.updated_at)" in skill


def test_notification_skill_uses_a_bounded_repository_acknowledgement() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert "repos/$GITHUB_REPOSITORY/notifications" in skill
    assert 'last_read_at="$ACK_CUTOFF"' in skill
    assert '"$UNRESOLVED_AT -1 second"' in skill
    assert 'if [ -n "$UNRESOLVED_AT" ]; then' in skill
    assert "else\n  ACK_CUTOFF=$CUTOFF" in skill
    assert "Never acknowledge a same-repository thread individually." in skill
    assert "notifications/threads/<thread-id>" in skill


def test_notification_skill_pins_the_fragile_dedup_queries() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert ".display_title == $title" in skill
    assert "issues/$NUMBER/timeline?per_page=100" in skill
    assert '.event == "cross-referenced"' in skill


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
    assert "whose live head has no bot review" in skill
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
    precheck = _read("generator", "src", "tend", "templates", "notifications-check.sh")

    for content in (install, precheck):
        assert "repos/$REPO/subscription" in content or (
            "repos/$GITHUB_REPOSITORY/subscription" in content
        )
        assert "-F subscribed=true -F ignored=false" in content


def test_review_skill_retargets_a_moved_head_rather_than_discarding_it() -> None:
    """A push mid-review re-targets the review rather than throwing it away.

    Re-targeting requires the live head to build on the reviewed one, and every
    review pins the commit it read: unpinned, GitHub anchors it at whatever is
    live when the POST lands, so the review claims code the session never saw.

    The sha reaches the POST through a file because it cannot reach it any
    other way — the agent composes the body between reading the head and
    posting, and shell state does not survive a tool call.
    """
    skill = _read("plugins", "tend-ci-runner", "skills", "review", "SKILL.md")

    assert 'git merge-base --is-ancestor "$HEAD_SHA" "$CURRENT_HEAD"' in skill
    assert "HEAD moved — leaving" not in skill

    # Written where the head is read, and rewritten where it moves.
    assert 'echo "$HEAD_SHA" > /tmp/reviewed-head' in skill
    assert 'echo "$CURRENT_HEAD" > /tmp/reviewed-head' in skill
    # Read back by both posting recipes, and read *before* the POST: inlined as
    # `$(cat ...)` a missing file substitutes the empty string and the request
    # still goes out, which is the unpinned review the pin exists to prevent.
    assert skill.count("REVIEWED=$(cat /tmp/reviewed-head) || exit 0") == 2
    assert '-f commit_id="$REVIEWED"' in skill
    assert '--arg sha "$REVIEWED"' in skill

    # Three commands read the delta, and dropping any one of them silently
    # narrows what the session sees rather than failing.
    #
    # The scoped log is the author's own new code: a plain two-dot diff between
    # the heads would hand the session everything a base merge dragged in.
    assert "git log -p --no-merges" in skill
    assert '--not "$BASE_SHA"' in skill
    # The merges log is the only place a base merge appears, and it carries a
    # label or it reads as one more commit in the scoped log's stream.
    assert '--merges "$HEAD_SHA..$CURRENT_HEAD"' in skill
    assert "base merge: %h %s" in skill
    # `--cc` is the only place a conflicted merge's resolution appears: the
    # author commits it inside the merge, where neither log reaches it. It is
    # not a substitute for re-verifying, though — a resolution taking the base
    # side prints no hunks, and a clean merge that only shifts lines prints
    # nothing anywhere, so the override below has to be unconditional.
    assert "git show --cc" in skill
    assert "even if the scoped log printed nothing" in skill


def test_weekly_approval_pins_the_commit_it_checked() -> None:
    """Weekly approves dependency PRs, the population `nightly` rewrites on
    purpose, so an unpinned approval lands on a commit nothing checked. It
    carries the sha the same way review does, and for the same reason: the
    body is composed with the Write tool in between."""
    weekly = _read("plugins", "tend-ci-runner", "skills", "weekly", "SKILL.md")

    # Per-PR and cleared up front: step 2 loops over every dependency PR, so a
    # shared name hands the next PR this one's sha, and the already-approved
    # branch must not leave a readable file behind.
    assert "rm -f /tmp/checked-head-<number>" in weekly
    assert 'echo "$HEAD_SHA" > /tmp/checked-head-<number>' in weekly
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
    used to read. Both approval paths — step 5's no-issues approve and the
    incremental path's "your findings are now addressed" approve — have to
    reach the gate, so each carries a pointer to it.
    """
    skill = _read("plugins", "tend-ci-runner", "skills", "review", "SKILL.md")

    # Stated once, under step 5, where every approving path is sent for the
    # POST recipe.
    assert "**Unless the author withheld merge readiness.**" in skill
    # The bot's own findings closing out is what fired the wrong approval:
    # the two conditions are independent and only the author clears the second.
    assert "independent conditions" in skill

    # The incremental path approves without reading step 5's prose, so the
    # pointer rides on the sentence that prescribes the approval.
    assert "so the PR isn't left in limbo — and the author-readiness gate" in skill

    # A blocker can also arrive mid-session, after the review began, so the
    # pre-APPROVE peek re-checks it alongside the red-check gate.
    assert "Re-check the author-readiness gate" in skill
