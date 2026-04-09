"""Smoke tests for workflow generation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from click.testing import CliRunner

from tend.cli import main
from tend.config import Config
from tend.workflows import GENERATORS, generate_all, generate_mention


def _minimal_config(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / ".config" / "tend.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'bot_name = "test-bot"\n{extra}')
    return cfg


def test_minimal_config_generates_seven_workflows(tmp_path: Path) -> None:
    """ci-fix requires watched_workflows, so minimal config produces seven."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = generate_all(cfg)
    assert len(workflows) == 7
    names = {wf.filename for wf in workflows}
    assert names == {
        "tend-review.yaml",
        "tend-mention.yaml",
        "tend-triage.yaml",
        "tend-nightly.yaml",
        "tend-weekly.yaml",
        "tend-notifications.yaml",
        "tend-review-runs.yaml",
    }


def test_generated_yaml_is_valid(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_config(tmp_path))
    for wf in generate_all(cfg):
        data = yaml.safe_load(wf.content)
        assert isinstance(data, dict), f"{wf.filename} did not parse as dict"
        assert "name" in data, f"{wf.filename} missing name"
        assert "jobs" in data, f"{wf.filename} missing jobs"


def test_disabled_workflow_not_generated(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_config(tmp_path, "[workflows.weekly]\nenabled = false"))
    workflows = generate_all(cfg)
    names = {wf.filename for wf in workflows}
    assert "tend-weekly.yaml" not in names
    assert len(workflows) == 6


def test_setup_steps_rendered(tmp_path: Path) -> None:
    extra = dedent("""\
        setup = [
          {uses = "./.github/actions/my-setup"},
          {run = "echo FOO=bar >> $GITHUB_ENV"},
        ]
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert "./.github/actions/my-setup" in wf.content, (
            f"{wf.filename} missing uses step"
        )
        assert "echo FOO=bar >> $GITHUB_ENV" in wf.content, (
            f"{wf.filename} missing run step"
        )


def test_empty_setup_no_blank_lines(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_config(tmp_path))
    for wf in generate_all(cfg):
        assert "\n\n\n" not in wf.content, f"{wf.filename} has triple blank lines"


def test_custom_secrets(tmp_path: Path) -> None:
    extra = dedent("""\
        [secrets]
        bot_token = "MY_BOT_PAT"
        claude_token = "MY_CLAUDE"
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert "MY_BOT_PAT" in wf.content, f"{wf.filename} missing custom bot token"
        assert "MY_CLAUDE" in wf.content, f"{wf.filename} missing custom claude token"


def test_custom_prompt(tmp_path: Path) -> None:
    extra = dedent("""\
        [workflows.triage]
        prompt = "Custom triage: {issue_number}"
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    triage = workflows["tend-triage.yaml"]
    assert "Custom triage:" in triage.content


def test_watched_workflows(tmp_path: Path) -> None:
    extra = dedent("""\
        [workflows.ci-fix]
        watched_workflows = ["build", "test", "lint"]
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    ci_fix = workflows["tend-ci-fix.yaml"]
    assert '"build"' in ci_fix.content
    assert '"test"' in ci_fix.content
    assert '"lint"' in ci_fix.content
    assert 'branches: ["main"]' in ci_fix.content


def test_ci_fix_custom_branches(tmp_path: Path) -> None:
    extra = dedent("""\
        [workflows.ci-fix]
        watched_workflows = ["ci"]
        branches = ["main", "release"]
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    ci_fix = workflows["tend-ci-fix.yaml"]
    assert 'branches: ["main", "release"]' in ci_fix.content


def test_cli_init_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dry-run"])
    assert result.exit_code == 0
    assert "tend-review.yaml" in result.output
    # Dry run should not create files
    assert not (tmp_path / ".github" / "workflows").exists()


def test_cli_init_writes_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0
    assert "Generated 7 workflow files" in result.output
    wf_dir = tmp_path / ".github" / "workflows"
    assert wf_dir.exists()
    assert len(list(wf_dir.glob("tend-*.yaml"))) == 7


def test_setup_after_checkout_in_review(tmp_path: Path) -> None:
    """Setup steps must run after checkout, not before."""
    extra = 'setup = [{uses = "./.github/actions/my-setup"}]'
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    review = workflows["tend-review.yaml"]
    checkout_idx = review.content.index("actions/checkout@v6")
    setup_idx = review.content.index("./.github/actions/my-setup")
    assert setup_idx > checkout_idx, "Setup must come after checkout"


def test_setup_raw_yaml_injected(tmp_path: Path) -> None:
    extra = dedent('''\
        setup = [
          {raw = """
        - uses: Swatinem/rust-cache@v2
          with:
            save-if: false
        - run: cargo binstall cargo-insta --no-confirm
          shell: bash
        """},
        ]
    ''')
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        data = yaml.safe_load(wf.content)
        assert isinstance(data, dict), f"{wf.filename} did not parse as valid YAML"
        assert "Swatinem/rust-cache@v2" in wf.content, (
            f"{wf.filename} missing raw uses step"
        )
        assert "save-if: false" in wf.content, f"{wf.filename} missing with parameter"
        assert "cargo binstall" in wf.content, f"{wf.filename} missing raw run step"


def test_setup_raw_interleaved_with_steps(tmp_path: Path) -> None:
    extra = dedent('''\
        setup = [
          {uses = "./.github/actions/my-setup"},
          {raw = """
        - uses: Swatinem/rust-cache@v2
          with:
            save-if: false
        """},
          {run = "echo FOO=bar >> $GITHUB_ENV"},
        ]
    ''')
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert "./.github/actions/my-setup" in wf.content
        assert "Swatinem/rust-cache@v2" in wf.content
        assert "save-if: false" in wf.content
        assert "echo FOO=bar" in wf.content
        # Order preserved: uses, raw, run
        uses_idx = wf.content.index("./.github/actions/my-setup")
        raw_idx = wf.content.index("Swatinem/rust-cache@v2")
        run_idx = wf.content.index("echo FOO=bar")
        assert uses_idx < raw_idx < run_idx, f"{wf.filename}: wrong order"


def test_mention_handles_pull_request_review(tmp_path: Path) -> None:
    """pull_request_review (submitted) must be covered by tend-mention so the bot
    responds when a reviewer submits a formal review on an engaged PR."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)

    # Event trigger present
    assert "pull_request_review" in data[True], (
        "tend-mention must listen for pull_request_review events"
    )
    assert data[True]["pull_request_review"] == {"types": ["submitted"]}

    # Verify job filters out fork PRs for review events — secrets are
    # unavailable there. The notifications workflow polls for these.
    verify_if = data["jobs"]["verify"]["if"]
    assert "pull_request_review" in verify_if
    assert "issue_comment" in verify_if
    assert "comment.user.login" in verify_if
    assert "pull_request.head.repo.full_name" in verify_if

    # Handle job checks out PR branch for this event
    handle_steps = data["jobs"]["handle"]["steps"]
    checkout_step = next(
        s for s in handle_steps if s.get("name") == "Check out PR branch"
    )
    assert "pull_request_review" in checkout_step["if"]

    # Prompt includes review-specific branches
    tend_step = next(
        s for s in handle_steps if s.get("uses", "").startswith("max-sixty/tend@")
    )
    prompt = tend_step["with"]["prompt"]
    assert "github.event.review.html_url" in prompt
    assert "github.event.review.body" in prompt


def test_mention_verify_no_concurrency(tmp_path: Path) -> None:
    """verify job must not have concurrency — a non-mention comment can cancel
    an explicit @bot mention if both arrive on the same PR within seconds (#93)."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)
    verify = data["jobs"]["verify"]
    assert "concurrency" not in verify, (
        "verify job must not have concurrency — rapid comments on the same PR "
        "can cancel an explicit @bot mention (#93)"
    )


def test_mention_handle_job_queues_not_cancels(tmp_path: Path) -> None:
    """The handle job must queue (not cancel) to avoid dropping mentions (#93)."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)
    handle = data["jobs"]["handle"]
    assert "concurrency" in handle, (
        "handle job must have concurrency to prevent duplicate runs"
    )
    assert handle["concurrency"]["cancel-in-progress"] is False, (
        "handle must queue (cancel-in-progress: false) so mentions aren't dropped"
    )


def test_setup_after_pr_checkout_in_mention(tmp_path: Path) -> None:
    """Setup steps must run after PR checkout, not before."""
    extra = 'setup = [{uses = "./.github/actions/my-setup"}]'
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    # Setup should come after "Check out PR branch"
    checkout_idx = mention.content.index("Check out PR branch")
    setup_idx = mention.content.index("./.github/actions/my-setup")
    assert setup_idx > checkout_idx, "Setup must come after PR checkout"


def test_mention_handle_has_queue_delay(tmp_path: Path) -> None:
    """Handle job computes queue delay so the prompt can detect stale triggers."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)
    handle_steps = data["jobs"]["handle"]["steps"]
    delay_steps = [s for s in handle_steps if s.get("id") == "delay"]
    assert len(delay_steps) == 1, "handle job must have a queue delay step"
    assert "steps.delay.outputs.seconds" in mention.content, (
        "prompt must reference queue delay"
    )
    # Delay step must come before the tend action (output must be available)
    delay_idx = mention.content.index("Compute queue delay")
    tend_idx = mention.content.index("max-sixty/tend@v1")
    assert delay_idx < tend_idx, "delay step must precede tend action"


def test_mention_queue_delay_guards_empty_event_ts(tmp_path: Path) -> None:
    """date -d "" silently returns now on GNU; guard against empty EVENT_TS."""
    cfg = Config.load(_minimal_config(tmp_path))
    wf = generate_mention(cfg)
    data = yaml.safe_load(wf.content)
    delay_step = next(
        s for s in data["jobs"]["handle"]["steps"] if s.get("id") == "delay"
    )
    script = delay_step["run"]
    # Must bail before date -d when EVENT_TS is empty
    assert 'if [ -z "$EVENT_TS" ]' in script
    # date -d must only run after the guard
    guard_pos = script.index('-z "$EVENT_TS"')
    date_pos = script.index("date -d")
    assert guard_pos < date_pos, "empty guard must precede date -d call"


def test_mention_prompt_omits_delay_when_empty(tmp_path: Path) -> None:
    """Prompt preamble must not hardcode delay text — it should be conditional
    so an empty seconds output doesn't produce broken prose like 's after'."""
    cfg = Config.load(_minimal_config(tmp_path))
    wf = generate_mention(cfg)
    data = yaml.safe_load(wf.content)
    tend_step = next(
        s
        for s in data["jobs"]["handle"]["steps"]
        if s.get("uses", "").startswith("max-sixty/tend@")
    )
    prompt = tend_step["with"]["prompt"]
    # The delay text must be inside a format() conditional, not hardcoded
    assert "format(" in prompt, "delay preamble must use conditional format()"
    # "Before acting" must always appear (it's the unconditional part)
    assert "Before acting" in prompt


# ---------------------------------------------------------------------------
# Regtest snapshots — full YAML output for every workflow
# ---------------------------------------------------------------------------


def _extra_for(name: str) -> str:
    """Return extra config needed for a specific generator (e.g. ci-fix)."""
    if name == "ci-fix":
        return '[workflows.ci-fix]\nwatched_workflows = ["ci"]'
    return ""


@pytest.mark.parametrize("name", GENERATORS)
def test_workflow_minimal_regtest(regtest: object, tmp_path: Path, name: str) -> None:
    """Snapshot each workflow's full YAML with minimal config."""
    cfg = Config.load(_minimal_config(tmp_path, _extra_for(name)))
    wf = GENERATORS[name](cfg)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", GENERATORS)
def test_workflow_with_setup_regtest(
    regtest: object, tmp_path: Path, name: str
) -> None:
    """Snapshot each workflow's full YAML with a setup step."""
    extra = 'setup = [{uses = "astral-sh/setup-uv@v6"}]'
    extra_cfg = _extra_for(name)
    if extra_cfg:
        extra += "\n" + extra_cfg
    cfg = Config.load(_minimal_config(tmp_path, extra))
    wf = GENERATORS[name](cfg)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]
