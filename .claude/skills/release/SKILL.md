---
name: release
description: Tend release workflow. Use when user asks to "do a release", "release a new version", "cut a release", or wants to publish a new version to PyPI.
metadata:
  internal: true
---

# Release Workflow

## Steps

1. **Run tests and lints**: `cd generator && uv run pytest` and `pre-commit run --all-files`
2. **Check current version**: Read `version` in `generator/pyproject.toml`
3. **Review commits**: `git log <last-version>..HEAD --oneline` to understand scope
4. **Confirm version with user**: Present changes summary and proposed version
5. **Bump version**: Edit `version` in `generator/pyproject.toml`, then `cd generator && uv lock`
6. **Commit**: `chore: release X.Y.Z` with a one-line summary of changes since last release
7. **Merge to main**: Create PR via `gh pr create`, wait for CI, merge with `gh pr merge --squash`
8. **Tag and push**: `git tag X.Y.Z && git push origin X.Y.Z` (triggers PyPI release workflow in `.github/workflows/pypi-release.yaml`)
9. **Wait for PyPI release**: Poll the release workflow until `uvx tend@X.Y.Z --help` succeeds
10. **Regenerate tend's own workflows**: Run `uvx tend@latest init` and open a PR titled `chore: regenerate workflows with tend X.Y.Z`. Until this merges, tend's deployed workflows lag the just-released generator, so critical fixes (e.g. loop-prevention filters) remain unreachable on tend itself.

## Version scheme

Tags are bare versions (`0.0.9`), not prefixed (`v0.0.9`).

## Commit message pattern

```
chore: release X.Y.Z

Bumps generator version to X.Y.Z and syncs lockfile.

N commits since A.B.C: <brief list of notable changes with PR numbers>.
```
