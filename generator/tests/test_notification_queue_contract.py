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


def test_notification_skill_acknowledges_without_racing_new_activity() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "notifications", "SKILL.md")

    assert "repos/$GITHUB_REPOSITORY/notifications" in skill
    assert 'last_read_at="$CUTOFF"' in skill
    assert "Never acknowledge a same-repository thread individually." in skill
    assert "notifications/threads/<thread-id>" in skill


def test_installation_and_each_poll_enable_repository_watching() -> None:
    install = _read("plugins", "install-tend", "skills", "install-tend", "SKILL.md")
    precheck = _read("generator", "src", "tend", "templates", "notifications-check.sh")

    for content in (install, precheck):
        assert "repos/$REPO/subscription" in content or (
            "repos/$GITHUB_REPOSITORY/subscription" in content
        )
        assert "-F subscribed=true -F ignored=false" in content


def test_review_runs_reconciles_current_state_without_historical_reruns() -> None:
    skill = _read("plugins", "tend-ci-runner", "skills", "review-runs", "SKILL.md")

    assert "Reconcile live work" in skill
    assert "current-state scan" in skill
    assert "gh run rerun" not in skill
