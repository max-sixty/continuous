"""Tests for the .config/tend.toml → .config/tend.yaml migration."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from click import ClickException
from click.testing import CliRunner
from tend.cli import main
from tend.config import Config
from tend.migrate import migrate_toml_to_yaml
from tend.workflows import generate_all

from tests import _yaml as yaml


def _write_toml(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / ".config" / "tend.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(content)
    return cfg


def test_migrate_minimal_config(tmp_path: Path) -> None:
    """Minimal TOML → YAML: bot_name only."""
    toml_path = _write_toml(tmp_path, 'bot_name = "my-bot"\n')
    yaml_path = tmp_path / ".config" / "tend.yaml"

    migrate_toml_to_yaml(toml_path, yaml_path)

    assert not toml_path.exists(), "TOML must be deleted after successful migration"
    assert yaml_path.exists(), "YAML must be written"
    assert yaml.safe_load(yaml_path.read_text()) == {"bot_name": "my-bot"}


def test_migrate_full_config_round_trips_through_config_load(tmp_path: Path) -> None:
    """A realistic TOML config survives migration and produces a working Config."""
    toml_path = _write_toml(
        tmp_path,
        dedent("""\
        bot_name = "test-bot"
        protected_branches = ["v1", "v2"]
        model = "sonnet"

        [secrets]
        allowed = ["CODECOV_TOKEN"]

        [[setup]]
        run = "uv --version"

        [[setup]]
        run = "echo hi"
        env = { FOO = "bar" }

        [workflows.ci-fix]
        watched_workflows = ["ci"]

        [workflows.review.jobs.review]
        timeout-minutes = 240
    """),
    )
    yaml_path = tmp_path / ".config" / "tend.yaml"

    migrate_toml_to_yaml(toml_path, yaml_path)

    cfg = Config.load(yaml_path)
    assert cfg.bot_name == "test-bot"
    assert cfg.protected_branches == ["v1", "v2"]
    assert cfg.model == "sonnet"
    assert cfg.allowed_repo_secrets == ["CODECOV_TOKEN"]
    assert len(cfg.setup) == 2
    assert cfg.setup[0].fields == {"run": "uv --version"}
    assert cfg.setup[1].fields == {"run": "echo hi", "env": {"FOO": "bar"}}
    assert cfg.workflows["ci-fix"].watched_workflows == ["ci"]
    assert cfg.workflows["review"].jobs == {"review": {"timeout-minutes": 240}}


def test_migrate_generates_identical_workflows(tmp_path: Path) -> None:
    """The whole point: workflows produced from the migrated config must be
    byte-identical to what the TOML would have produced.

    We can't load the TOML directly with the new Config.load (TOML support
    is gone), so we simulate the equivalent: construct a Config from the
    parsed TOML dict via the same path Config.load uses internally.
    Equality of generated workflow content is the strongest check that
    migration is data-faithful.
    """
    import tomllib

    toml_text = dedent("""\
        bot_name = "test-bot"
        protected_branches = ["v1"]

        [[setup]]
        run = "uv --version"

        [workflows.ci-fix]
        watched_workflows = ["ci"]
    """)
    toml_path = _write_toml(tmp_path, toml_text)
    yaml_path = tmp_path / ".config" / "tend.yaml"

    # Capture what the TOML data structure looks like, then migrate.
    with toml_path.open("rb") as f:
        toml_data = tomllib.load(f)

    migrate_toml_to_yaml(toml_path, yaml_path)

    # The parsed YAML must equal the parsed TOML — same input to Config.load,
    # therefore identical Config, therefore identical generated workflows.
    yaml_data = yaml.safe_load(yaml_path.read_text())
    assert yaml_data == toml_data


def test_migrate_refuses_to_overwrite_existing_yaml(tmp_path: Path) -> None:
    """If .config/tend.yaml already exists, the migration aborts without
    touching either file."""
    toml_path = _write_toml(tmp_path, 'bot_name = "my-bot"\n')
    yaml_path = tmp_path / ".config" / "tend.yaml"
    yaml_path.write_text("bot_name: existing\n")

    with pytest.raises(ClickException, match="already exists"):
        migrate_toml_to_yaml(toml_path, yaml_path)

    # Both files left in place
    assert toml_path.exists()
    assert yaml_path.read_text() == "bot_name: existing\n"


def test_init_auto_migrates_legacy_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tend init` against a stale .toml-only repo migrates then generates."""
    _write_toml(
        tmp_path,
        dedent("""\
        bot_name = "test-bot"
        [workflows.ci-fix]
        watched_workflows = ["ci"]
    """),
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    assert "Migrated" in result.output
    assert not (tmp_path / ".config" / "tend.toml").exists()
    assert (tmp_path / ".config" / "tend.yaml").exists()

    wf_dir = tmp_path / ".github" / "workflows"
    assert (wf_dir / "tend-ci-fix.yaml").exists()
    assert (wf_dir / "tend-review.yaml").exists()


def test_init_dry_run_previews_the_migration_without_performing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`init --dry-run` writes nothing — including the migration.

    The preview run is the one an adopter makes to see what the upgrade would
    do, so performing the migration there converts the config behind their
    back and deletes the TOML they were still deciding about.
    """
    _write_toml(
        tmp_path,
        dedent("""\
        bot_name = "test-bot"
        [workflows.ci-fix]
        watched_workflows = ["ci"]
    """),
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init", "--dry-run"])
    assert result.exit_code == 0, result.output

    # Neither config file moved.
    assert (tmp_path / ".config" / "tend.toml").exists()
    assert not (tmp_path / ".config" / "tend.yaml").exists()
    # …and no workflows were written either.
    assert not (tmp_path / ".github" / "workflows").exists()

    # The preview still reports the migration and renders the workflows the
    # migrated config would produce — otherwise it previews nothing useful.
    assert "would migrate" in result.output
    assert "tend-review.yaml" in result.output
    assert "test-bot" in result.output


def test_init_does_not_touch_yaml_when_both_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both .toml and .yaml are present, `init` ignores the TOML — the
    YAML is canonical. Surfaces would-be data drift via the migration
    rather than silently picking one."""
    _write_toml(tmp_path, 'bot_name = "stale-bot"\n')
    yaml_path = tmp_path / ".config" / "tend.yaml"
    yaml_path.write_text("bot_name: real-bot\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    assert "Migrated" not in result.output
    # TOML left in place (operator can review and delete manually)
    assert (tmp_path / ".config" / "tend.toml").exists()
    assert yaml_path.read_text() == "bot_name: real-bot\n"
    # YAML's bot_name is the one that flowed into the workflows
    assert (
        "real-bot"
        in (tmp_path / ".github" / "workflows" / "tend-review.yaml").read_text()
    )


def test_check_errors_clearly_when_only_toml_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tend check` is read-only — it doesn't auto-migrate. The error
    points the user at `tend init`, which does."""
    _write_toml(tmp_path, 'bot_name = "test-bot"\n')
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["check"])
    assert result.exit_code != 0
    assert "tend@latest init" in result.output


def test_workflows_from_migrated_config_match_workflows_built_from_toml_dict(
    tmp_path: Path,
) -> None:
    """End-to-end equivalence: workflows generated after migration are
    byte-identical to those built directly from the parsed-TOML dict.

    This is the strongest version of `does migration still work well?` —
    not just dict equality, but full workflow content equality.
    """
    import tomllib

    toml_text = dedent("""\
        bot_name = "test-bot"
        protected_branches = ["v1"]

        [[setup]]
        run = "uv --version"

        [workflows.ci-fix]
        watched_workflows = ["ci"]

        [workflows.review.jobs.review]
        timeout-minutes = 240
    """)
    toml_path = _write_toml(tmp_path, toml_text)
    yaml_path = tmp_path / ".config" / "tend.yaml"

    # Build "before" workflows by loading the YAML we'd get if we just
    # serialized the TOML directly — that's the data structure both paths
    # converge on, so this is a valid baseline.
    with toml_path.open("rb") as f:
        toml_data = tomllib.load(f)
    pre_yaml = tmp_path / "_pre.yaml"
    pre_yaml.write_text(yaml.safe_dump(toml_data))
    cfg_pre = Config.load(pre_yaml)
    pre_workflows = {wf.filename: wf.content for wf in generate_all(cfg_pre)}

    # Migrate, regenerate.
    migrate_toml_to_yaml(toml_path, yaml_path)
    cfg_post = Config.load(yaml_path)
    post_workflows = {wf.filename: wf.content for wf in generate_all(cfg_post)}

    assert pre_workflows == post_workflows
