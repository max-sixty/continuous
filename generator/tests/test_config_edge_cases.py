"""Adversarial edge-case tests for the config parser."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from click import ClickException
from tend.config import BOT_TOKEN_SECRET, Config
from tend.workflows import generate_all

from tests import _yaml as yaml
from tests import agent_prompt as _agent_prompt


def _write_config(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / ".config" / "tend.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(content)
    return cfg


# ---------------------------------------------------------------------------
# 1. Empty config file
# ---------------------------------------------------------------------------


def test_empty_config_raises(tmp_path: Path) -> None:
    """Empty file has no bot_name -- must raise a clear error."""
    path = _write_config(tmp_path, "")
    with pytest.raises(ClickException, match="Missing required field: bot_name"):
        Config.load(path)


# ---------------------------------------------------------------------------
# 2. bot_name only -- minimal valid config
# ---------------------------------------------------------------------------


def test_bot_name_only(tmp_path: Path) -> None:
    """Minimal config with just bot_name should produce valid defaults."""
    path = _write_config(tmp_path, "bot_name: my-bot")
    cfg = Config.load(path)
    assert cfg.bot_name == "my-bot"
    assert cfg.enabled is True
    assert cfg.model == "opus"
    assert cfg.protected_branches == []
    assert cfg.setup == []
    assert cfg.workflows == {}
    assert cfg.allowed_repo_secrets == []
    assert cfg.memory_gist is False


@pytest.mark.parametrize("value", ["yes", '"false"', "1", "null", "{}"])
def test_enabled_requires_a_boolean(tmp_path: Path, value: str) -> None:
    path = _write_config(tmp_path, f"bot_name: my-bot\nenabled: {value}\n")
    with pytest.raises(ClickException, match="enabled must be true or false"):
        Config.load(path)


def test_yaml_merge_keys_are_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "defaults: &defaults\n  enabled: false\n<<: *defaults\nbot_name: my-bot\n",
    )

    with pytest.raises(ClickException, match="YAML merge keys"):
        Config.load(path)


@pytest.mark.parametrize("value", ["yes", "1", "{}"])
def test_memory_gist_requires_a_boolean(tmp_path: Path, value: str) -> None:
    path = _write_config(tmp_path, f"bot_name: my-bot\nmemory_gist: {value}\n")
    with pytest.raises(ClickException, match="must be true or false"):
        Config.load(path)


def test_memory_gist_requires_a_claude_workflow(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        harness: codex
        model: gpt-5.5
        memory_gist: true
    """),
    )
    with pytest.raises(ClickException, match="requires at least one enabled workflow"):
        Config.load(path)


def test_memory_gist_ignores_a_disabled_top_level_harness(
    tmp_path: Path,
) -> None:
    disabled = "\n".join(
        f"  {name}: false"
        for name in (
            "review",
            "mention",
            "triage",
            "ci-fix",
            "nightly",
            "weekly",
            "notifications",
            "review-runs",
        )
    )
    path = _write_config(
        tmp_path,
        f"bot_name: my-bot\nmemory_gist: true\nworkflows:\n{disabled}\n",
    )

    with pytest.raises(ClickException, match="requires at least one enabled workflow"):
        Config.load(path)


# ---------------------------------------------------------------------------
# 2b. protected_branches
# ---------------------------------------------------------------------------


def test_protected_branches_parsed(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        protected_branches: ["v1", "v2"]
    """),
    )
    cfg = Config.load(path)
    assert cfg.protected_branches == ["v1", "v2"]


def test_protected_branches_empty_list(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        protected_branches: []
    """),
    )
    cfg = Config.load(path)
    assert cfg.protected_branches == []


def test_protected_branches_non_list_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        protected_branches: "v1"
    """),
    )
    with pytest.raises(ClickException, match="protected_branches must be a list"):
        Config.load(path)


def test_protected_branches_empty_string_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        protected_branches: ["v1", ""]
    """),
    )
    with pytest.raises(ClickException, match="non-empty strings"):
        Config.load(path)


# ---------------------------------------------------------------------------
# 2c. model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["opus", "sonnet", "haiku"])
def test_model_accepted(tmp_path: Path, model: str) -> None:
    path = _write_config(tmp_path, f"bot_name: my-bot\nmodel: {model}\n")
    cfg = Config.load(path)
    assert cfg.model == model


def test_model_unknown_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "bot_name: my-bot\nmodel: gpt-4\n")
    with pytest.raises(ClickException, match="model 'gpt-4' is not recognized"):
        Config.load(path)


def test_model_appears_in_generated_workflows(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "bot_name: my-bot\nmodel: opus\n")
    cfg = Config.load(path)
    for wf in generate_all(cfg):
        assert "model: opus" in wf.content, f"{wf.filename} missing model"


# ---------------------------------------------------------------------------
# 3. Unknown top-level keys
# ---------------------------------------------------------------------------


def test_unknown_top_level_keys_warned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Extra keys like foo: bar should produce a warning."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        foo: bar
        some_future_field: 42
    """),
    )
    cfg = Config.load(path)
    assert cfg.bot_name == "my-bot"
    captured = capsys.readouterr()
    assert "Warning: unknown config key 'foo'" in captured.err
    assert "Warning: unknown config key 'some_future_field'" in captured.err


# ---------------------------------------------------------------------------
# 4. Workflow as boolean vs dict
# ---------------------------------------------------------------------------


def test_workflow_dict_enabled_false(tmp_path: Path) -> None:
    """workflows.review.enabled: false -- mapping form."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          review:
            enabled: false
    """),
    )
    cfg = Config.load(path)
    assert cfg.workflows["review"].enabled is False


def test_workflow_boolean_false(tmp_path: Path) -> None:
    """workflows.review: false -- shorthand boolean form."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          review: false
    """),
    )
    cfg = Config.load(path)
    assert cfg.workflows["review"].enabled is False


def test_workflow_boolean_true(tmp_path: Path) -> None:
    """workflows.review: true -- shorthand boolean form."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          review: true
    """),
    )
    cfg = Config.load(path)
    assert cfg.workflows["review"].enabled is True


# ---------------------------------------------------------------------------
# 5. Empty string values
# ---------------------------------------------------------------------------


def test_empty_bot_name_rejected(tmp_path: Path) -> None:
    """bot_name: '' must be rejected."""
    path = _write_config(tmp_path, 'bot_name: ""')
    with pytest.raises(ClickException, match="bot_name must not be empty"):
        Config.load(path)


def test_empty_cron(tmp_path: Path) -> None:
    """cron: '' -- the cron field falls back to empty, which the
    _generate_scheduled function handles via `wf.cron or default_cron`.
    Empty string is falsy, so it correctly falls back to the default."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          nightly:
            cron: ""
    """),
    )
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
    path = _write_config(tmp_path, 'bot_name: "my bot"')
    with pytest.raises(ClickException, match="not a valid GitHub username"):
        Config.load(path)


def test_bot_name_with_at_sign_rejected(tmp_path: Path) -> None:
    """At-sign is not valid in GitHub usernames."""
    path = _write_config(tmp_path, 'bot_name: "bot@123"')
    with pytest.raises(ClickException, match="not a valid GitHub username"):
        Config.load(path)


@pytest.mark.parametrize(
    "yaml_value",
    [
        '"O\'Brien"',
        '"bot\\"name"',
        '"bot{0}"',
        '"bot\\nname"',
    ],
)
def test_bot_name_with_special_chars_rejected(tmp_path: Path, yaml_value: str) -> None:
    """Special characters are not valid GitHub usernames."""
    path = _write_config(tmp_path, f"bot_name: {yaml_value}\n")
    with pytest.raises(ClickException, match="not a valid GitHub username"):
        Config.load(path)


def test_bot_name_with_hyphens_valid(tmp_path: Path) -> None:
    """Hyphens are valid in GitHub usernames."""
    path = _write_config(tmp_path, "bot_name: my-project-bot")
    cfg = Config.load(path)
    assert cfg.bot_name == "my-project-bot"


# ---------------------------------------------------------------------------
# 7. Custom prompt punctuation: braces and quotes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", ["review", "triage"])
def test_prompt_punctuation_reaches_the_agent_verbatim(
    tmp_path: Path, workflow: str
) -> None:
    """Braces and quotes in a prompt are the adopter's own text, not syntax.

    Every prompt is a YAML block scalar, so the only substitution is the
    workflow's own `{...}` placeholder. `{0}`, `{1}` and a stray apostrophe once
    had to be escaped for review, whose prompt was a GitHub Actions
    `format('...')` expression — and escaping was correct only when the
    placeholder was present, so the same prompt shipped different text depending
    on an unrelated part of itself.
    """
    placeholder = "pr_number" if workflow == "review" else "issue_number"
    path = _write_config(
        tmp_path,
        dedent(f"""\
        bot_name: my-bot
        workflows:
          {workflow}:
            prompt: "Don't touch {{0}} or {{1}}; fix {{{placeholder}}}"
    """),
    )
    workflows = {wf.filename: wf for wf in generate_all(Config.load(path))}
    content = workflows[f"tend-{workflow}.yaml"].content
    assert isinstance(yaml.safe_load(content), dict)
    prompt = _agent_prompt(content)
    assert "format(" not in prompt
    assert prompt.startswith("Don't touch {0} or {1}; fix ${{ github.event.")


# ---------------------------------------------------------------------------
# 9. Very long prompt
# ---------------------------------------------------------------------------


def test_very_long_prompt(tmp_path: Path) -> None:
    """2000+ character prompt -- should not break YAML generation."""
    long_text = "x" * 2500
    path = _write_config(
        tmp_path,
        dedent(f"""\
        bot_name: my-bot
        workflows:
          review:
            prompt: "{long_text}"
    """),
    )
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
    path = _write_config(
        tmp_path,
        dedent(f"""\
        bot_name: my-bot
        workflows:
          nightly:
            prompt: "{long_text}"
    """),
    )
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
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        setup:
          - uses: ./.github/actions/setup
          - uses: ./.github/actions/setup
    """),
    )
    cfg = Config.load(path)
    assert len(cfg.setup) == 2
    assert cfg.setup[0].fields == {"uses": "./.github/actions/setup"}
    assert cfg.setup[1].fields == {"uses": "./.github/actions/setup"}
    # Both duplicates appear in generated YAML
    workflows = generate_all(cfg)
    for wf in workflows:
        count = wf.content.count("./.github/actions/setup")
        assert count == 2, f"{wf.filename} has {count} setup steps, expected 2"


# ---------------------------------------------------------------------------
# 11. watched_workflows empty list
# ---------------------------------------------------------------------------


def test_watched_workflows_empty_list_rejected(tmp_path: Path) -> None:
    """watched_workflows: [] is rejected — workflow_run needs at least one."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          ci-fix:
            watched_workflows: []
    """),
    )
    with pytest.raises(ClickException, match="watched_workflows.*invalid"):
        Config.load(path)


@pytest.mark.parametrize(
    "key,value",
    [
        # A scalar is the natural typo, and it used to render verbatim into the
        # trigger as `workflows: "ci"` / `branches: "main"`.
        ("watched_workflows", "ci"),
        ("branches", "main"),
        # A number or bool reached `len()` and raised a bare TypeError.
        ("watched_workflows", "5"),
        ("branches", "true"),
        # A mapping has a length, so it passed the empty-list check and
        # rendered as a JSON object.
        ("watched_workflows", "{a: b}"),
        # An element that isn't a non-empty string renders as `null` / `""`.
        ("watched_workflows", '["ci", null]'),
        ("branches", '["main", ""]'),
    ],
)
def test_workflow_list_fields_reject_non_list_of_strings(
    tmp_path: Path, key: str, value: str
) -> None:
    """`watched_workflows` and `branches` go straight into `workflow_run:`."""
    # Only one entry per key — a duplicate mapping key is its own YAML error.
    keys = {"watched_workflows": '["ci"]', key: value}
    body = "".join(f"    {k}: {v}\n" for k, v in keys.items())
    path = _write_config(
        tmp_path,
        f"bot_name: my-bot\nworkflows:\n  ci-fix:\n{body}",
    )
    with pytest.raises(
        ClickException, match=rf"workflows\.ci-fix\.{key} must be a list"
    ):
        Config.load(path)


def test_branches_empty_list_rejected(tmp_path: Path) -> None:
    """branches: [] renders a trigger that matches no branch."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          ci-fix:
            watched_workflows: ["ci"]
            branches: []
    """),
    )
    with pytest.raises(ClickException, match="branches: .. matches no branch"):
        Config.load(path)


def test_branches_explicit_value(tmp_path: Path) -> None:
    """A valid `branches` list still reaches the rendered trigger."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          ci-fix:
            watched_workflows: ["ci"]
            branches: ["main", "release"]
    """),
    )
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    assert 'branches: ["main", "release"]' in workflows["tend-ci-fix.yaml"].content


def test_watched_workflows_explicit_value(tmp_path: Path) -> None:
    """Explicit watched_workflows should be used, no fallback."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          ci-fix:
            watched_workflows: ["build"]
    """),
    )
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    ci_fix = workflows["tend-ci-fix.yaml"]
    assert '"build"' in ci_fix.content
    assert '"ci"' not in ci_fix.content


def test_watched_workflows_missing_raises_on_direct_call(tmp_path: Path) -> None:
    """generate_ci_fix errors when watched_workflows is not configured."""
    from tend.workflows import generate_ci_fix

    path = _write_config(tmp_path, "bot_name: my-bot")
    cfg = Config.load(path)
    with pytest.raises(ClickException, match="ci-fix requires watched_workflows"):
        generate_ci_fix(cfg)


def test_watched_workflows_missing_skips_ci_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ci-fix is skipped with a warning when watched_workflows is not configured."""
    path = _write_config(tmp_path, "bot_name: my-bot")
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    assert "tend-ci-fix.yaml" not in workflows
    captured = capsys.readouterr()
    assert "Skipping ci-fix" in captured.err
    assert "watched_workflows" in captured.err


# ---------------------------------------------------------------------------
# Additional edge cases discovered during analysis
# ---------------------------------------------------------------------------


def test_renovate_renamed_to_weekly(tmp_path: Path) -> None:
    """Old workflows.renovate key must fail with a clear rename error."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          renovate:
            cron: "0 12 * * 0"
    """),
    )
    with pytest.raises(ClickException, match="renamed to workflows.weekly"):
        Config.load(path)


def test_unknown_workflow_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown workflow names should produce a warning on stderr."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          nonexistent:
            enabled: true
    """),
    )
    Config.load(path)
    captured = capsys.readouterr()
    assert "Warning: unknown workflow 'nonexistent'" in captured.err


def test_prompt_with_multiline(tmp_path: Path) -> None:
    """Multi-line prompt -- YAML block scalar."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          nightly:
            prompt: |
              Line one.
              Line two.
              Line three.
    """),
    )
    cfg = Config.load(path)
    assert "Line one." in cfg.workflows["nightly"].prompt
    assert "Line two." in cfg.workflows["nightly"].prompt
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    nightly = workflows["tend-nightly.yaml"]
    data = yaml.safe_load(nightly.content)
    assert isinstance(data, dict)


def test_bot_name_yaml_injection_rejected(tmp_path: Path) -> None:
    """bot_name containing ': ' is not a valid GitHub username — rejected."""
    path = _write_config(tmp_path, 'bot_name: "bot: name"')
    with pytest.raises(ClickException, match="not a valid GitHub username"):
        Config.load(path)


# ---------------------------------------------------------------------------
# setup steps — ordered sequence
# ---------------------------------------------------------------------------


def test_setup_steps_preserves_order(tmp_path: Path) -> None:
    """setup as a YAML sequence preserves interleaved uses/run order."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        setup:
          - uses: ./.github/actions/setup-node
          - run: echo middle
          - uses: ./.github/actions/setup-cache
    """),
    )
    cfg = Config.load(path)
    assert len(cfg.setup) == 3
    assert cfg.setup[0].fields == {"uses": "./.github/actions/setup-node"}
    assert cfg.setup[1].fields == {"run": "echo middle"}
    assert cfg.setup[2].fields == {"uses": "./.github/actions/setup-cache"}
    # Verify order in generated YAML
    workflows = generate_all(cfg)
    for wf in workflows:
        node_pos = wf.content.index("setup-node")
        middle_pos = wf.content.index("echo middle")
        cache_pos = wf.content.index("setup-cache")
        assert node_pos < middle_pos < cache_pos, f"Order wrong in {wf.filename}"


def test_setup_steps_empty_list(tmp_path: Path) -> None:
    """setup: [] produces no setup steps."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        setup: []
    """),
    )
    cfg = Config.load(path)
    assert cfg.setup == []


def test_setup_steps_entry_missing_key(tmp_path: Path) -> None:
    """setup entry without uses or run is rejected."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        setup:
          - name: oops
    """),
    )
    with pytest.raises(ClickException, match="setup\\[0\\] must have exactly one"):
        Config.load(path)


def test_setup_steps_entry_both_keys(tmp_path: Path) -> None:
    """setup entry with both uses and run is rejected."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        setup:
          - uses: action
            run: cmd
    """),
    )
    with pytest.raises(ClickException, match="setup\\[0\\] must have exactly one"):
        Config.load(path)


@pytest.mark.parametrize("value", ["false", "true", "0", "[]", "null", "''", "'   '"])
def test_setup_step_if_requires_non_empty_string(tmp_path: Path, value: str) -> None:
    path = _write_config(
        tmp_path,
        dedent(f"""\
        bot_name: my-bot
        setup:
          - run: echo hi
            if: {value}
    """),
    )

    with pytest.raises(ClickException, match="`if` must be a non-empty string"):
        Config.load(path)


@pytest.mark.parametrize(
    "condition",
    [
        "${{ runner.os == 'Linux' }} && ${{ github.event_name == 'push' }}",
        "runner.os == 'Linux' || ${{ github.event_name == 'push' }}",
        "${{ runner.os == 'Linux' }} || github.event_name == 'push'",
    ],
)
def test_setup_step_if_rejects_mixed_expression_wrappers(
    tmp_path: Path, condition: str
) -> None:
    path = _write_config(
        tmp_path,
        dedent(f'''\
        bot_name: my-bot
        setup:
          - run: echo hi
            if: "{condition}"
    '''),
    )

    with pytest.raises(ClickException, match="plain expression or one whole"):
        Config.load(path)


def test_setup_step_if_rejects_empty_expression_wrapper(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        setup:
          - run: echo hi
            if: "${{ }}"
    """),
    )

    with pytest.raises(ClickException, match="`if` must contain an expression"):
        Config.load(path)


def test_workflow_disabled_boolean_shorthand_not_generated(tmp_path: Path) -> None:
    """Boolean shorthand `review: false` should prevent generation."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          review: false
    """),
    )
    cfg = Config.load(path)
    workflows = generate_all(cfg)
    names = {wf.filename for wf in workflows}
    assert "tend-review.yaml" not in names


# ---------------------------------------------------------------------------
# secrets.allowed
# ---------------------------------------------------------------------------


def test_allowed_secrets_parsed(tmp_path: Path) -> None:
    """secrets.allowed is parsed as a list of secret names."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        secrets:
          allowed: ["CODECOV_TOKEN", "SENTRY_DSN"]
    """),
    )
    cfg = Config.load(path)
    assert cfg.allowed_repo_secrets == ["CODECOV_TOKEN", "SENTRY_DSN"]


def test_unknown_secrets_key_warned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Typos like secrets.aallowed should produce a warning."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        secrets:
          aallowed: ["PYPI_TOKEN"]
    """),
    )
    Config.load(path)
    captured = capsys.readouterr()
    assert "Warning: unknown secrets key 'aallowed'" in captured.err


def test_allowed_secrets_string_rejected(tmp_path: Path) -> None:
    """secrets.allowed: 'CODECOV_TOKEN' (string) must be rejected."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        secrets:
          allowed: "CODECOV_TOKEN"
    """),
    )
    with pytest.raises(ClickException, match="secrets.allowed must be a list"):
        Config.load(path)


def test_allowed_secrets_refuses_operational_names(tmp_path: Path) -> None:
    """Allowlisting an operational secret would let a repo-level copy pass
    `tend check` — one config line quietly re-opening what the environment
    gate closes — so the config is refused at the edge."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        secrets:
          allowed: ["CODECOV_TOKEN", "TEND_BOT_TOKEN"]
    """),
    )
    with pytest.raises(ClickException, match=BOT_TOKEN_SECRET):
        Config.load(path)


def test_secret_name_override_refused(tmp_path: Path) -> None:
    """The per-adopter name overrides are gone. Ignoring a leftover one would
    generate workflows reading the fixed name while the adopter's secret still
    answers to the old one, so it fails with the rename to make."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        secrets:
          bot_token: MY_BOT_PAT
    """),
    )
    with pytest.raises(
        ClickException, match=rf"secrets\.bot_token → {BOT_TOKEN_SECRET}"
    ):
        Config.load(path)


# ---------------------------------------------------------------------------
# YAML-specific: legacy TOML file fails with a clear error
# ---------------------------------------------------------------------------


def test_legacy_toml_file_errors_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `.config/tend.toml` exists but `.config/tend.yaml` doesn't, the
    error must explicitly tell the user to rename and translate."""
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "tend.toml").write_text('bot_name = "my-bot"\n')
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ClickException, match="tend now reads .*tend\\.yaml"):
        Config.load()


# ---------------------------------------------------------------------------
# YAML 1.2 semantics: `on` and other reserved-in-1.1 words round-trip as strings
# ---------------------------------------------------------------------------


def test_unquoted_on_key_round_trips_as_string(tmp_path: Path) -> None:
    """Unquoted `on:` in a user override stays a string under YAML 1.2.
    Under YAML 1.1 (PyYAML), it would parse as boolean True and collide with
    the workflow's own `on:` trigger key — ruamel.yaml/1.2 avoids that."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          nightly:
            workflow_extra:
              on:
                schedule: null
    """),
    )
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    nightly = yaml.safe_load(workflows["tend-nightly.yaml"].content)
    triggers = nightly["on"]
    # schedule was deleted via JSON Merge Patch `null`
    assert "schedule" not in triggers
    # workflow_dispatch survives
    assert "workflow_dispatch" in triggers


def test_norway_problem_string_not_coerced(tmp_path: Path) -> None:
    """Under YAML 1.1 (PyYAML), unquoted `NO`/`yes`/`on`/`off` are booleans.
    Under YAML 1.2 they're strings — a prompt or env value containing one
    of those tokens survives unscathed."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          review:
            workflow_extra:
              env:
                COUNTRY_CODE: NO
                FEATURE_FLAG: off
    """),
    )
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    env = yaml.safe_load(workflows["tend-review.yaml"].content)["env"]
    assert env["COUNTRY_CODE"] == "NO"
    assert env["FEATURE_FLAG"] == "off"


def test_workflow_extra_delete_with_null(tmp_path: Path) -> None:
    """YAML-native null deletes a key via RFC 7396 JSON Merge Patch — the
    primary motivation for switching from TOML."""
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        workflows:
          review:
            jobs:
              review:
                permissions:
                  issues: null
    """),
    )
    cfg = Config.load(path)
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    perms = yaml.safe_load(workflows["tend-review.yaml"].content)["jobs"]["review"][
        "permissions"
    ]
    # issues was deleted; the other permissions survive
    assert "issues" not in perms
    assert perms["contents"] == "write"


# ---------------------------------------------------------------------------
# Sandbox levers (sandbox_path / sandbox_env / sandbox_setup)
# ---------------------------------------------------------------------------


def test_sandbox_levers_parsed(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_path:
          - ~/.cargo/bin
        sandbox_env:
          RUST_BACKTRACE: "1"
          CARGO_TERM_COLOR: always
        sandbox_setup:
          - rustup component add clippy
    """),
    )
    cfg = Config.load(path)
    assert cfg.sandbox_path == ["~/.cargo/bin"]
    # Scalar `1` coerces to its string form for the NAME=VALUE env line.
    assert cfg.sandbox_env == {"RUST_BACKTRACE": "1", "CARGO_TERM_COLOR": "always"}
    assert cfg.sandbox_setup == ["rustup component add clippy"]


def test_sandbox_env_coerces_scalar_value(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_env:
          RUST_BACKTRACE: 1
    """),
    )
    cfg = Config.load(path)
    assert cfg.sandbox_env == {"RUST_BACKTRACE": "1"}


def test_sandbox_env_coerces_boolean_lowercase(tmp_path: Path) -> None:
    # `bool` is an `int` subclass; emit shell-conventional lowercase, not
    # Python's `True`/`False`.
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_env:
          DEBUG: true
          QUIET: false
    """),
    )
    cfg = Config.load(path)
    assert cfg.sandbox_env == {"DEBUG": "true", "QUIET": "false"}


def test_sandbox_env_newline_value_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_env:
          FOO: "a\\nb"
    """),
    )
    with pytest.raises(ClickException, match="must be a single line"):
        Config.load(path)


def test_sandbox_path_newline_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_path:
          - "~/.cargo/bin\\n~/evil"
    """),
    )
    with pytest.raises(ClickException, match="single line"):
        Config.load(path)


def test_sandbox_env_non_scalar_value_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_env:
          FOO: [a, b]
    """),
    )
    with pytest.raises(ClickException, match="must be a scalar"):
        Config.load(path)


def test_sandbox_env_reserved_key_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_env:
          HTTPS_PROXY: http://evil
    """),
    )
    with pytest.raises(ClickException, match="reserved key 'HTTPS_PROXY'"):
        Config.load(path)


def test_sandbox_env_path_rejected_points_to_sandbox_path(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_env:
          PATH: /whatever
    """),
    )
    with pytest.raises(ClickException, match="sandbox_path"):
        Config.load(path)


def test_sandbox_env_invalid_name_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        sandbox_env:
          "1BAD": x
    """),
    )
    with pytest.raises(ClickException, match="not a valid environment"):
        Config.load(path)


def test_sandbox_path_non_list_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "bot_name: my-bot\nsandbox_path: ~/.cargo/bin\n",
    )
    with pytest.raises(ClickException, match="sandbox_path must be a list"):
        Config.load(path)


def test_sandbox_setup_non_list_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "bot_name: my-bot\nsandbox_setup: echo hi\n",
    )
    with pytest.raises(ClickException, match="sandbox_setup must be a list"):
        Config.load(path)


def test_sandbox_levers_apply_to_codex(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        harness: codex
        model: gpt-5.5
        sandbox_setup:
          - echo hi
    """),
    )
    Config.load(path)
    assert "apply only to the Claude harness" not in capsys.readouterr().err


def test_sandbox_levers_no_warn_with_claude_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(
        tmp_path,
        dedent("""\
        bot_name: my-bot
        harness: codex
        model: gpt-5.5
        sandbox_setup:
          - echo hi
        workflows:
          review:
            harness: claude
            model: opus
    """),
    )
    Config.load(path)
    assert "apply only to the Claude harness" not in capsys.readouterr().err
