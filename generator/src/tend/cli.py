"""CLI for generating tend workflow files."""

from __future__ import annotations

from pathlib import Path

import click

from tend.checks import CheckResult, detect_repo, fix_branch_protection, run_all_checks
from tend.config import Config
from tend.workflows import generate_all


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
    """Generate Claude-powered CI workflows from .config/tend.toml."""


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
        click.echo("Run `tend check` to verify security prerequisites.")


@main.command()
@click.option("--config", "-c", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--repo", "-r", help="GitHub repo (owner/name). Auto-detected if omitted.")
@click.option("--fix", is_flag=True, help="Fix failing checks (creates rulesets, etc.)")
def check(config_path: Path | None, repo: str | None, fix: bool) -> None:
    """Verify security prerequisites (branch protection, bot access, secrets)."""
    cfg = Config.load(config_path)
    results = run_all_checks(cfg, repo)

    click.echo("Security checks:")
    _print_check_results(results)

    failures = [r for r in results if r.passed is False]
    if not failures:
        return

    if not fix:
        raise SystemExit(1)

    # Resolve repo for fix operations.
    if repo is None:
        repo = detect_repo()
    if repo is None:
        click.echo("Could not detect repo — pass --repo to fix.")
        raise SystemExit(1)

    fixed_any = False
    for r in failures:
        if r.name == "branch-protection" and "bot can still merge" in r.message:
            click.echo()
            click.echo("Creating 'Merge access' ruleset — only admins can merge to default branch...")
            fix_result = fix_branch_protection(repo)
            _print_check_results([fix_result])
            if fix_result.passed:
                fixed_any = True

    if fixed_any:
        click.echo()
        click.echo("Re-running checks...")
        results = run_all_checks(cfg, repo)
        _print_check_results(results)
        if any(r.passed is False for r in results):
            raise SystemExit(1)
    else:
        raise SystemExit(1)
