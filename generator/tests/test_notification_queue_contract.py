"""Pin the notification queue's cross-file safety contract."""

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
    assert "if ! OUTAGE=$(gh issue list" in skill
    assert "--json body,comments --jq '.body, .comments[].body'" in skill
    assert "gh issue close <outage-number> --reason completed" in skill
    assert "complete **Reconcile live work** below, then exit" in skill
    assert "Do not replay historical workflow runs" in skill
    assert "an open issue with no bot response to the latest human activity" in skill
    assert "whose live head has no bot review" in skill
    assert "failing default-branch CI with no bot fix in progress" in skill


def test_unreadable_notification_subjects_are_terminal() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert "whose `subject.url` is null" in skill
    assert "a deleted issue or PR" in skill
    assert "also has the outcome “no action”" in skill


def test_installation_and_each_poll_enable_repository_watching() -> None:
    install = _read("plugins", "install-tend", "skills", "install-tend", "SKILL.md")
    precheck = _read("generator", "src", "tend", "templates", "notifications-check.sh")

    for content in (install, precheck):
        assert "repos/$REPO/subscription" in content or (
            "repos/$GITHUB_REPOSITORY/subscription" in content
        )
        assert "-F subscribed=true -F ignored=false" in content
