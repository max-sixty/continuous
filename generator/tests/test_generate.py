"""Smoke tests for workflow generation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from click.testing import CliRunner

from tend.cli import main
from tend.config import Config
from tend.workflows import generate_all


def _minimal_config(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / ".config" / "tend.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'bot_name = "test-bot"\n{extra}')
    return cfg


def test_minimal_config_generates_five_workflows(tmp_path: Path) -> None:
    """ci-fix requires watched_workflows, so minimal config produces five."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = generate_all(cfg)
    assert len(workflows) == 5
    names = {wf.filename for wf in workflows}
    assert names == {
        "tend-review.yaml",
        "tend-mention.yaml",
        "tend-triage.yaml",
        "tend-nightly.yaml",
        "tend-renovate.yaml",
    }


def test_generated_yaml_is_valid(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_config(tmp_path))
    for wf in generate_all(cfg):
        data = yaml.safe_load(wf.content)
        assert isinstance(data, dict), f"{wf.filename} did not parse as dict"
        assert "name" in data, f"{wf.filename} missing name"
        assert "jobs" in data, f"{wf.filename} missing jobs"


def test_disabled_workflow_not_generated(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_config(tmp_path, "[workflows.renovate]\nenabled = false"))
    workflows = generate_all(cfg)
    names = {wf.filename for wf in workflows}
    assert "tend-renovate.yaml" not in names
    assert len(workflows) == 4


def test_setup_steps_rendered(tmp_path: Path) -> None:
    extra = dedent("""\
        setup = [
          {uses = "./.github/actions/my-setup"},
          {run = "echo FOO=bar >> $GITHUB_ENV"},
        ]
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert "./.github/actions/my-setup" in wf.content, f"{wf.filename} missing uses step"
        assert 'echo FOO=bar >> $GITHUB_ENV' in wf.content, f"{wf.filename} missing run step"


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
    assert "Generated 5 workflow files" in result.output
    wf_dir = tmp_path / ".github" / "workflows"
    assert wf_dir.exists()
    assert len(list(wf_dir.glob("tend-*.yaml"))) == 5


def test_setup_after_pr_checkout_in_review(tmp_path: Path) -> None:
    """Setup steps must run after PR checkout, not before."""
    extra = 'setup = [{uses = "./.github/actions/my-setup"}]'
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    review = workflows["tend-review.yaml"]
    # Setup should come after "Check out PR branch"
    checkout_idx = review.content.index("Check out PR branch")
    setup_idx = review.content.index("./.github/actions/my-setup")
    assert setup_idx > checkout_idx, "Setup must come after PR checkout"


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
        assert "Swatinem/rust-cache@v2" in wf.content, f"{wf.filename} missing raw uses step"
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
