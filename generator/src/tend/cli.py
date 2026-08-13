"""CLI for generating tend workflow files."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import click

from tend.checks import (
    CheckResult,
    admitted_refs,
    detect_canonical_owner,
    detect_default_branch,
    detect_repo,
    fix_branch_protection,
    fix_environment,
    run_all_checks,
)
from tend.config import Config
from tend.migrate import migrate_toml_to_yaml, render_toml_as_yaml
from tend.workflows import generate_all


def _detect_default_branch_local() -> str:
    """Detect the default branch from the local git remote."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Returns "origin/main" or "origin/master" — strip the remote prefix
            ref = result.stdout.strip()
            if "/" in ref:
                return ref.split("/", 1)[1]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "main"


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
    """An autonomous junior maintainer for GitHub repos, powered by Claude or OpenAI Codex. Generates and manages workflows from .config/tend.yaml."""


@main.command()
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
)
@click.option("--dry-run", is_flag=True, help="Print generated files without writing")
@click.option(
    "--with-install-test",
    is_flag=True,
    help=(
        "Also generate tend-install-test.yaml, a one-shot workflow that runs "
        "on the install PR to verify secrets and generator drift. The install "
        "skill passes this flag during initial setup; the nightly regen runs "
        "without it so the file is removed once the install PR has landed."
    ),
)
def init(config_path: Path | None, dry_run: bool, with_install_test: bool) -> None:
    """Generate workflow files from config. Idempotent — always overwrites."""
    # Auto-migrate a legacy .config/tend.toml. One-shot upgrade path for
    # adopters bumping past the TOML→YAML cutover; the migration verifies
    # the parsed structures match before swapping, so the no-op case (no
    # .toml on disk) is the steady state.
    #
    # Under --dry-run the migration is rendered and verified but not applied:
    # the flag's contract is that the command writes nothing, and the one run
    # an adopter makes to preview the upgrade is exactly the run that would
    # otherwise perform it — silently, and irreversibly for an uncommitted
    # config, since the migration deletes the TOML.
    preview_yaml: str | None = None
    if config_path is None:
        default_yaml = Path(".config/tend.yaml")
        default_toml = Path(".config/tend.toml")
        if not default_yaml.exists() and default_toml.exists():
            if dry_run:
                preview_yaml = render_toml_as_yaml(default_toml, default_yaml)
                click.echo(f"  would migrate {default_toml} → {default_yaml}")
            else:
                migrate_toml_to_yaml(default_toml, default_yaml)
                click.echo(f"Migrated {default_toml} → {default_yaml}")

    if preview_yaml is None:
        cfg = Config.load(config_path)
    else:
        # `Config.load` reads a path, and the real one must stay unwritten —
        # so the preview loads from a throwaway copy. Same bytes the migration
        # would have committed, so the workflows printed below are the ones a
        # real `init` would produce.
        with tempfile.TemporaryDirectory() as tmp:
            preview_path = Path(tmp) / "tend.yaml"
            preview_path.write_text(preview_yaml)
            cfg = Config.load(preview_path)
    cfg.default_branch = _detect_default_branch_local()
    cfg.repo_owner = detect_canonical_owner() or ""
    if not cfg.repo_owner:
        click.echo(
            "Warning: could not detect the canonical repo owner via `gh` "
            "(install gh and run `gh repo set-default` if multiple remotes "
            "are configured). Generated workflows will not include the fork "
            "guard, so jobs may fail noisily if a contributor runs them from "
            "a fork.",
            err=True,
        )
    outdir = Path(".github/workflows")

    workflows = generate_all(cfg, with_install_test=with_install_test)

    if workflows and not dry_run:
        outdir.mkdir(parents=True, exist_ok=True)

    for wf in workflows:
        path = outdir / wf.filename
        if dry_run:
            click.echo(f"--- {wf.filename} ---")
            click.echo(wf.content)
            continue

        path.write_text(wf.content)
        click.echo(f"  wrote {path}")

    # Remove stale tend-*.yaml files the generator didn't produce this run.
    # Catches: install-test cleanup on regen, disabled workflows leaving
    # behind their YAML, and workflows renamed across generator versions.
    # Runs even when the generated set is empty (every workflow disabled)
    # so the cleanup contract still applies. The tend-*.yaml glob is the
    # generator's filename contract per CLAUDE.md — adopter-owned workflows
    # live under other names.
    generated = {wf.filename for wf in workflows}
    removed = 0
    for path in sorted(outdir.glob("tend-*.yaml")):
        if path.name in generated:
            continue
        if dry_run:
            click.echo(f"  would remove {path}")
        else:
            path.unlink()
            click.echo(f"  removed {path}")
        removed += 1

    if dry_run:
        return

    if not workflows:
        suffix = f" Removed {removed} stale tend-*.yaml file(s)." if removed else ""
        click.echo(f"No workflows enabled in config.{suffix}")
        return

    suffix = f" ({removed} removed)" if removed else ""
    click.echo(f"\nGenerated {len(workflows)} workflow files{suffix}.")
    click.echo("Run `tend check` to verify security prerequisites.")


@main.command()
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
)
@click.option(
    "--repo", "-r", help="GitHub repo (owner/name). Auto-detected if omitted."
)
@click.option("--fix", is_flag=True, help="Fix failing checks (creates rulesets, etc.)")
def check(config_path: Path | None, repo: str | None, fix: bool) -> None:
    """Verify security prerequisites (branch protection, bot access, credentials)."""
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

    # Every fix is written in terms of the default branch, and guessing it
    # wrong writes a ref the bot can create: a `main` guessed for a `master`
    # repo is outside the merge restriction, so the environment would admit a
    # branch the bot can push. The checks just resolved it, so a failure here
    # is a transient one to surface, not to paper over.
    default_branch = detect_default_branch(repo)
    if default_branch is None:
        click.echo(f"Could not detect the default branch for {repo} — not fixing.")
        raise SystemExit(1)

    fixed_any = False
    bp_fixable = [
        r
        for r in failures
        if r.name.startswith("branch-protection:")
        and "bot can still merge" in r.message
    ]
    if bp_fixable:
        branches_desc = ", ".join(r.name.split(":", 1)[1] for r in bp_fixable)
        click.echo()
        click.echo(
            f"Creating 'Merge access' ruleset — only admins can merge ({branches_desc})..."
        )
        fix_result = fix_branch_protection(repo, default_branch, cfg.protected_branches)
        _print_check_results([fix_result])
        if fix_result.passed:
            fixed_any = True
            # The environment's admitted set is read off the branch-protection
            # results, which the ruleset just changed. Re-read them, or the
            # policy would be written from the pre-fix picture — for a repo
            # whose only failure was the missing ruleset, an empty one.
            results = run_all_checks(cfg, repo)
            failures = [r for r in results if r.passed is False]

    if any(r.name == "environment" for r in failures):
        click.echo()
        click.echo("Configuring the 'tend' environment...")
        # Read off the same run's branch-protection results, so the policy the
        # fix writes is the set the check just demanded — never a ref whose
        # protection this run could not verify. An empty set can't reach here:
        # the check reports unknown rather than failure when nothing verified.
        fix_result = fix_environment(repo, admitted_refs(results))
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
