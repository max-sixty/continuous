"""Adversarial edge-case tests for the config parser."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from click import ClickException

from tend.config import Config, WorkflowConfig
from tend.workflows import generate_all


def _write_config(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / ".config" / "tend.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(content)
    return cfg


# ---------------------------------------------------------------------------
# 1. Empty TOML file
# ---------------------------------------------------------------------------


def test_empty_toml_raises(tmp_path: Path) -> None:
    """Empty file has no bot_name -- must raise a clear error."""
    path = _write_config(tmp_path, "")
    with pytest.raises(ClickException, match="Missing required field: bot_name"):
        Config.load(path)


# ---------------------------------------------------------------------------
# 2. bot_name only -- minimal valid config
# ---------------------------------------------------------------------------


def test_bot_name_only(tmp_path: Path) -> None:
    """Minimal config with just bot_name should produce valid defaults."""
    path = _write_config(tmp_path, 'bot_name = "my-bot"')
    cfg = Config.load(path)
    assert cfg.bot_name == "my-bot"
    assert cfg.bot_token_secret == "BOT_TOKEN"
    assert cfg.claude_token_secret == "CLAUDE_CODE_OAUTH_TOKEN"
    assert cfg.setup == []
    assert cfg.workflows == {}


# ---------------------------------------------------------------------------
# 3. Unknown top-level keys
# ---------------------------------------------------------------------------


def test_unknown_top_level_keys_warned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Extra keys like foo = 'bar' should produce a warning."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        foo = "bar"
        some_future_field = 42
    """))
    cfg = Config.load(path)
    assert cfg.bot_name == "my-bot"
    captured = capsys.readouterr()
    assert "Warning: unknown config key 'foo'" in captured.err
    assert "Warning: unknown config key 'some_future_field'" in captured.err


# ---------------------------------------------------------------------------
# 4. Workflow as boolean vs dict
# ---------------------------------------------------------------------------


def test_workflow_dict_enabled_false(tmp_path: Path) -> None:
    """[workflows.review]\\n  enabled = false -- dict form."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.review]
        enabled = false
    """))
    cfg = Config.load(path)
    assert cfg.workflows["review"].enabled is False


def test_workflow_boolean_false(tmp_path: Path) -> None:
    """workflows.review = false -- shorthand boolean form."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows]
        review = false
    """))
    cfg = Config.load(path)
    assert cfg.workflows["review"].enabled is False


def test_workflow_boolean_true(tmp_path: Path) -> None:
    """workflows.review = true -- shorthand boolean form."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows]
        review = true
    """))
    cfg = Config.load(path)
    assert cfg.workflows["review"].enabled is True


# ---------------------------------------------------------------------------
# 5. Empty string values
# ---------------------------------------------------------------------------


def test_empty_bot_name_rejected(tmp_path: Path) -> None:
    """bot_name = '' must be rejected."""
    path = _write_config(tmp_path, 'bot_name = ""')
    with pytest.raises(ClickException, match="bot_name must not be empty"):
        Config.load(path)


def test_empty_cron(tmp_path: Path) -> None:
    """cron = '' -- the cron field falls back to empty, which the
    _generate_scheduled function handles via `wf.cron or default_cron`.
    Empty string is falsy, so it correctly falls back to the default."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.nightly]
        cron = ""
    """))
    cfg = Config.load(path)
    assert cfg.workflows["nightly"].cron == ""
    # Empty string is falsy, so the generator falls back to the default cron
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    nightly = workflows["tend-nightly.yaml"]
    assert "17 6 * * *" in nightly.content  # default cron used


# ---------------------------------------------------------------------------
# 6. Special characters in bot_name
# ---------------------------------------------------------------------------


def test_bot_name_with_spaces_rejected(tmp_path: Path) -> None:
    """Spaces in bot_name are not valid GitHub usernames."""
    path = _write_config(tmp_path, 'bot_name = "my bot"')
    with pytest.raises(ClickException, match="not a valid GitHub username"):
        Config.load(path)


def test_bot_name_with_at_sign_rejected(tmp_path: Path) -> None:
    """At-sign is not valid in GitHub usernames."""
    path = _write_config(tmp_path, 'bot_name = "bot@123"')
    with pytest.raises(ClickException, match="not a valid GitHub username"):
        Config.load(path)


@pytest.mark.parametrize("toml_value", [
    '''"O'Brien"''',
    r'''"bot\"name"''',
    '"bot{0}"',
    r'"bot\nname"',
])
def test_bot_name_with_special_chars_rejected(tmp_path: Path, toml_value: str) -> None:
    """Special characters are not valid GitHub usernames."""
    path = _write_config(tmp_path, f"bot_name = {toml_value}")
    with pytest.raises(ClickException, match="not a valid GitHub username"):
        Config.load(path)


def test_bot_name_with_hyphens_valid(tmp_path: Path) -> None:
    """Hyphens are valid in GitHub usernames."""
    path = _write_config(tmp_path, 'bot_name = "my-project-bot"')
    cfg = Config.load(path)
    assert cfg.bot_name == "my-project-bot"


# ---------------------------------------------------------------------------
# 7. Custom prompt with {0} pattern
# ---------------------------------------------------------------------------


def test_prompt_with_zero_placeholder(tmp_path: Path) -> None:
    """A prompt containing {0} — escaped so it doesn't collide with format()."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.review]
        prompt = "Fix {0} in {pr_number}"
    """))
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    review = workflows["tend-review.yaml"]
    # User's {0} is escaped to {{0}}, while {pr_number} becomes {0}
    assert "format(" in review.content
    assert "Fix {{0}} in {0}" in review.content


def test_prompt_with_numbered_placeholders(tmp_path: Path) -> None:
    """Prompt with {1}, {2} — escaped to prevent format() runtime errors."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.review]
        prompt = "Fix issue {1} and {2}"
    """))
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    review = workflows["tend-review.yaml"]
    # {1} and {2} are escaped to {{1}} and {{2}} — literals in format()
    assert "format(" in review.content
    assert "{{1}}" in review.content
    assert "{{2}}" in review.content


# ---------------------------------------------------------------------------
# 8. Custom prompt with single quotes
# ---------------------------------------------------------------------------


def test_prompt_with_single_quotes(tmp_path: Path) -> None:
    """Prompt containing single quotes -- the review workflow uses
    format('...') which needs '' escaping in GitHub Actions."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.review]
        prompt = "Don't break this"
    """))
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    review = workflows["tend-review.yaml"]
    # The _escape() function doubles single quotes for GHA expressions
    assert "Don''t" in review.content


def test_prompt_with_single_quotes_triage(tmp_path: Path) -> None:
    """Triage workflow does NOT use _escape() -- single quotes could break."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.triage]
        prompt = "Don't break {issue_number}"
    """))
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    triage = workflows["tend-triage.yaml"]
    # Triage uses YAML block scalar (prompt: |) so single quotes should be fine
    assert "Don't break" in triage.content
    data = yaml.safe_load(triage.content)
    assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# 9. Very long prompt
# ---------------------------------------------------------------------------


def test_very_long_prompt(tmp_path: Path) -> None:
    """2000+ character prompt -- should not break YAML generation."""
    long_text = "x" * 2500
    path = _write_config(tmp_path, dedent(f"""\
        bot_name = "my-bot"
        [workflows.review]
        prompt = "{long_text}"
    """))
    cfg = Config.load(path)
    assert len(cfg.workflows["review"].prompt) == 2500
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    review = workflows["tend-review.yaml"]
    data = yaml.safe_load(review.content)
    assert isinstance(data, dict)
    assert long_text in review.content


def test_very_long_prompt_nightly(tmp_path: Path) -> None:
    """Long prompt in nightly (scheduled) workflow uses block scalar."""
    long_text = "y" * 2500
    path = _write_config(tmp_path, dedent(f"""\
        bot_name = "my-bot"
        [workflows.nightly]
        prompt = "{long_text}"
    """))
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    nightly = workflows["tend-nightly.yaml"]
    data = yaml.safe_load(nightly.content)
    assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# 10. Duplicate setup steps
# ---------------------------------------------------------------------------


def test_duplicate_setup_steps_accepted(tmp_path: Path) -> None:
    """Duplicate uses entries are accepted without warning or dedup."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        setup = [
          {uses = "./.github/actions/setup"},
          {uses = "./.github/actions/setup"},
        ]
    """))
    cfg = Config.load(path)
    assert len(cfg.setup) == 2
    assert cfg.setup[0].uses == "./.github/actions/setup"
    assert cfg.setup[1].uses == "./.github/actions/setup"
    # Both duplicates appear in generated YAML
    workflows = generate_all(cfg)
    for wf in workflows:
        count = wf.content.count("./.github/actions/setup")
        assert count == 2, f"{wf.filename} has {count} setup steps, expected 2"


# ---------------------------------------------------------------------------
# 11. watched_workflows empty list
# ---------------------------------------------------------------------------


def test_watched_workflows_empty_list_rejected(tmp_path: Path) -> None:
    """watched_workflows = [] is rejected — workflow_run needs at least one."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.ci-fix]
        watched_workflows = []
    """))
    with pytest.raises(ClickException, match="watched_workflows.*invalid"):
        Config.load(path)


def test_watched_workflows_explicit_value(tmp_path: Path) -> None:
    """Explicit watched_workflows should be used, no fallback."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.ci-fix]
        watched_workflows = ["build"]
    """))
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    ci_fix = workflows["tend-ci-fix.yaml"]
    assert '"build"' in ci_fix.content
    assert '"ci"' not in ci_fix.content


# ---------------------------------------------------------------------------
# Additional edge cases discovered during analysis
# ---------------------------------------------------------------------------


def test_unknown_workflow_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Unknown workflow names should produce a warning on stderr."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows.nonexistent]
        enabled = true
    """))
    cfg = Config.load(path)
    captured = capsys.readouterr()
    assert "Warning: unknown workflow 'nonexistent'" in captured.err


def test_prompt_with_multiline(tmp_path: Path) -> None:
    """Multi-line prompt -- TOML multi-line basic string."""
    path = _write_config(tmp_path, dedent('''\
        bot_name = "my-bot"
        [workflows.nightly]
        prompt = """
        Line one.
        Line two.
        Line three.
        """
    '''))
    cfg = Config.load(path)
    assert "Line one." in cfg.workflows["nightly"].prompt
    assert "Line two." in cfg.workflows["nightly"].prompt
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    nightly = workflows["tend-nightly.yaml"]
    data = yaml.safe_load(nightly.content)
    assert isinstance(data, dict)


def test_bot_name_yaml_injection_rejected(tmp_path: Path) -> None:
    """bot_name containing ': ' is not a valid GitHub username — rejected."""
    path = _write_config(tmp_path, 'bot_name = "bot: name"')
    with pytest.raises(ClickException, match="not a valid GitHub username"):
        Config.load(path)


# ---------------------------------------------------------------------------
# setup.steps — ordered inline-table format
# ---------------------------------------------------------------------------


def test_setup_steps_preserves_order(tmp_path: Path) -> None:
    """setup = [{...}, ...] preserves interleaved uses/run order."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        setup = [
          {uses = "./.github/actions/setup-node"},
          {run = "echo middle"},
          {uses = "./.github/actions/setup-cache"},
        ]
    """))
    cfg = Config.load(path)
    assert len(cfg.setup) == 3
    assert cfg.setup[0].uses == "./.github/actions/setup-node"
    assert cfg.setup[1].run == "echo middle"
    assert cfg.setup[2].uses == "./.github/actions/setup-cache"
    # Verify order in generated YAML
    workflows = generate_all(cfg)
    for wf in workflows:
        node_pos = wf.content.index("setup-node")
        middle_pos = wf.content.index("echo middle")
        cache_pos = wf.content.index("setup-cache")
        assert node_pos < middle_pos < cache_pos, f"Order wrong in {wf.filename}"


def test_setup_steps_empty_list(tmp_path: Path) -> None:
    """setup = [] produces no setup steps."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        setup = []
    """))
    cfg = Config.load(path)
    assert cfg.setup == []


def test_setup_steps_entry_missing_key(tmp_path: Path) -> None:
    """setup entry without uses or run is rejected."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        setup = [{name = "oops"}]
    """))
    with pytest.raises(ClickException, match="setup\\[0\\] must have 'uses' or 'run'"):
        Config.load(path)


def test_setup_steps_entry_both_keys(tmp_path: Path) -> None:
    """setup entry with both uses and run is rejected."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        setup = [{uses = "action", run = "cmd"}]
    """))
    with pytest.raises(ClickException, match="setup\\[0\\] must have 'uses' or 'run', not both"):
        Config.load(path)



def test_workflow_disabled_boolean_shorthand_not_generated(tmp_path: Path) -> None:
    """Boolean shorthand `review = false` should prevent generation."""
    path = _write_config(tmp_path, dedent("""\
        bot_name = "my-bot"
        [workflows]
        review = false
    """))
    cfg = Config.load(path)
    workflows = generate_all(cfg)
    names = {wf.filename for wf in workflows}
    assert "tend-review.yaml" not in names
