"""One-shot migration from `.config/tend.toml` to `.config/tend.yaml`.

Bridges the format change introduced when tend's config switched from TOML
to YAML. The new code path doesn't read TOML anymore, so this command holds
the only `tomllib` import in the codebase — it exists solely to let
existing adopters upgrade without hand-editing.

Verification: the parsed TOML dict and the parsed-back YAML dict must
compare equal. If they don't, the migration aborts and the TOML file is
left in place. Equal dicts mean `Config.load` (which operates on the
parsed dict, not the file format) will produce an identical `Config`, and
therefore identical generated workflows.
"""

from __future__ import annotations

import io
import tomllib
from pathlib import Path

import click
from ruamel.yaml import YAML


def render_toml_as_yaml(toml_path: Path, yaml_path: Path) -> str:
    """Return `toml_path` rendered as verified-equivalent YAML text.

    Touches neither file — `yaml_path` is read only to refuse a migration that
    would overwrite it. This is the whole migration bar the two writes, so
    `tend init --dry-run` runs it to preview the upgrade on a config it must
    leave exactly as it found it.

    Raises `click.ClickException` if `yaml_path` already exists or the
    round-trip verification fails.
    """
    if yaml_path.exists():
        raise click.ClickException(
            f"{yaml_path} already exists — refusing to overwrite. "
            f"Delete it first if you want to re-migrate from {toml_path}."
        )

    with toml_path.open("rb") as f:
        toml_data = tomllib.load(f)

    yaml = YAML(typ="rt", pure=True)
    yaml.default_flow_style = False
    yaml.allow_unicode = True

    buf = io.StringIO()
    yaml.dump(toml_data, buf)
    yaml_text = buf.getvalue()

    # Verify by parsing the YAML back and comparing the structures. Equal
    # dicts mean Config.load — which works on the parsed dict — produces
    # the same Config, and therefore the same generated workflows.
    yaml_safe = YAML(typ="safe", pure=True)
    yaml_data = yaml_safe.load(yaml_text)

    if yaml_data != toml_data:
        raise click.ClickException(
            "Migration verification failed: parsed YAML does not match parsed TOML. "
            f"The TOML at {toml_path} is left in place; please report this as a "
            "bug.\n\n"
            f"TOML: {toml_data!r}\n"
            f"YAML: {yaml_data!r}"
        )

    return yaml_text


def migrate_toml_to_yaml(toml_path: Path, yaml_path: Path) -> None:
    """Read `toml_path`, write equivalent YAML at `yaml_path`, delete the TOML.

    Raises `click.ClickException` if `yaml_path` already exists or the
    round-trip verification fails.
    """
    yaml_text = render_toml_as_yaml(toml_path, yaml_path)
    yaml_path.write_text(yaml_text)
    toml_path.unlink()
