---
name: release
description: Tend release workflow. Use when user asks to "do a release", "release a new version", "cut a release", or wants to publish a new version to PyPI.
metadata:
  internal: true
---

# Release Workflow

## Steps

1. **Sync the release branch, then run tests and lints**: The `release` branch is long-lived and may sit behind `main` (or carry leftover state) when a cycle starts. Basing the changelog on a stale branch silently drops any commit merged to `main` after the branch was last realigned — `git log <last-version>..HEAD` won't show it. Bring it current first: `git fetch origin && git merge origin/main` (resolve any conflicts; if `release` has no commits of its own, `git reset --hard origin/main` instead). Then `wt test` and `uv tool run pre-commit run --all-files`.
2. **Check current version**: Read `version` in `generator/pyproject.toml`
3. **Review commits**: `git log <last-version>..origin/main --oneline` to understand scope — against `origin/main` (not `HEAD`), so the range is the full set of commits this release ships even if step 1 was skipped
4. **Confirm version with user**: Present changes summary and proposed version
5. **Bump version**: Edit `version` in `generator/pyproject.toml`, then `uv lock` at the repo root (the workspace's only lockfile)
6. **Update CHANGELOG**: Add a `## X.Y.Z` section at the top of `CHANGELOG.md` (see "CHANGELOG" below). The release workflow publishes this section verbatim as the GitHub Release notes and **fails the GitHub Release job if the section is missing** (PyPI publish has already happened by then; recovery is a manual `gh release create`), so it must land in the release commit — before the tag.
7. **Commit on the current branch**: `chore: release X.Y.Z` (version bump, lockfile, and CHANGELOG). Don't create a new branch — this worktree is already on the release branch, and the PR opens from it to `main`.
8. **Merge to main**: Push, create PR via `gh pr create`, wait for CI, merge with `gh pr merge --squash`
9. **Tag and push**: `git tag X.Y.Z && git push origin X.Y.Z` — triggers `.github/workflows/pypi-release.yaml`, which publishes to PyPI and creates a GitHub Release from the version's CHANGELOG section.
10. **Wait for the release workflow**: Poll until `uvx tend@X.Y.Z --help` succeeds and the release appears (`gh release view X.Y.Z`).
11. **Regenerate tend's own workflows**: Stay on the `release` branch (don't create a new one — same as step 7). The squash-merge deleted `origin/release`, so `git fetch && git reset --hard origin/main` to realign with the squashed history. Then `uvx tend@latest init`, commit, push, and open a PR titled `chore: regenerate workflows with tend X.Y.Z`. Until this merges, tend's deployed workflows lag the just-released generator, so critical fixes (e.g. loop-prevention filters) remain unreachable on tend itself.

## CHANGELOG

`CHANGELOG.md` holds one `## X.Y.Z` section per release, newest first. The header must be exactly `## X.Y.Z` — the release workflow matches it literally to extract the notes.

Draft the section from the commits since the last release (`git log <last-version>..origin/main --oneline`):

- **Group by section**, in order, omitting empty ones: **Improved**, **Fixed**, **Documentation**, **Internal**. Internal is for selected notable internals, not everything.
- **Combine related PRs** into one bullet; cite them all in a trailing `([#a](url), [#b](url))` list. Use full `https://github.com/max-sixty/tend/pull/N` URLs so links resolve from the GitHub Release page.
- **Be brief**: 1–3 sentences per bullet; Internal bullets terser.
- **No editorial framing**: describe what changed, not what was wrong with the old approach.
- **Verify against the diff**, not the commit subject — subjects often undersell or misdescribe. `git show <sha>` anything user-facing before trusting its bullet.

## Version scheme

Tags are bare versions (`0.0.9`), not prefixed (`v0.0.9`).

Generated workflows pin the harness action to the generator's own version
(`max-sixty/tend/claude@X.Y.Z`, `max-sixty/tend/codex@X.Y.Z`); there is no
bare-root action and no floating `v1`. Each `X.Y.Z` tag is the immutable ref
consumers run, enforced by a tag ruleset on `max-sixty/tend`
(`update`/`deletion` restricted). Never force-move or delete a published tag.
Step 9 (tag) must precede step 11 (regenerate via `uvx tend@latest`) so the
pinned ref resolves to an existing tag.

## Commit message pattern

```
chore: release X.Y.Z

Bumps generator version to X.Y.Z and syncs lockfile.

N commits since A.B.C: <brief list of notable changes with PR numbers>.
```
