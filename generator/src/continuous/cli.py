"""CLI for generating continuous workflow files."""

from __future__ import annotations

from pathlib import Path

import click

from continuous.checks import CheckResult, run_all_checks
from continuous.config import Config
from continuous.workflows import generate_all


def _print_check_results(results: list[CheckResult]) -> None:
    """Print check results with pass/fail/skip indicators."""
    for r in results:
        if r.passed is True:
            icon = click.style("PASS", fg="green")
        elif r.passed is False:
            icon = click.style("FAIL", fg="red")
        else:
            icon = click.style("SKIP", fg="yellow")
        click.echo(f"  {icon}  {r.name} — {r.message}")


@click.group()
def main() -> None:
    """Generate Claude-powered CI workflows from .config/continuous.toml."""


@main.command()
@click.option("--config", "-c", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--dry-run", is_flag=True, help="Print generated files without writing")
def init(config_path: Path | None, dry_run: bool) -> None:
    """Generate workflow files from config. Idempotent — always overwrites."""
    cfg = Config.load(config_path)
    outdir = Path(".github/workflows")

    workflows = generate_all(cfg)
    if not workflows:
        click.echo("No workflows enabled in config.")
        return

    if not dry_run:
        outdir.mkdir(parents=True, exist_ok=True)

    for wf in workflows:
        path = outdir / wf.filename
        if dry_run:
            click.echo(f"--- {wf.filename} ---")
            click.echo(wf.content)
            continue

        path.write_text(wf.content)
        click.echo(f"  wrote {path}")

    if not dry_run:
        click.echo(f"\nGenerated {len(workflows)} workflow files.")
        click.echo("Run `continuous check` to verify security prerequisites.")


@main.command()
@click.option("--config", "-c", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--repo", "-r", help="GitHub repo (owner/name). Auto-detected if omitted.")
def check(config_path: Path | None, repo: str | None) -> None:
    """Verify security prerequisites (branch protection, bot access, secrets)."""
    cfg = Config.load(config_path)
    results = run_all_checks(cfg, repo)

    click.echo("Security checks:")
    _print_check_results(results)

    failures = [r for r in results if r.passed is False]
    if failures:
        raise SystemExit(1)
