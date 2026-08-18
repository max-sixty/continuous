#!/usr/bin/env bash
# On a PR, the head checkout contains attacker-controlled files that the CLI
# reads at startup BEFORE any permission gating — SessionStart hooks, env-var
# overrides (NODE_OPTIONS, LD_PRELOAD, PATH), MCP servers, apiKeyHelper shell
# commands. Restore them from the PR base branch, which a maintainer reviewed
# and merged. Used by the Claude harness action.
#
# Path list and ordering mirror claude-code-action's restore-config.ts
# (src/github/operations/restore-config.ts). Snapshot PR-authored versions to
# .claude-pr/ first (excluded from git via info/exclude) so review skills can
# optionally inspect what the PR changed without those files ever being
# executed. Then delete (so an attacker-controlled .gitmodules can't stall the
# fetch on credential prompts), then fetch base, then check out each path, then
# unstage so the revert doesn't silently leak into commits Claude makes later.
#
# AGENTS.md is in the list for the prompt-injection half rather than the RCE
# half: Claude Code discovers it natively, not only through a CLAUDE.md
# `@`-import, so a repo that keeps CLAUDE.md as a one-line pointer at AGENTS.md
# would otherwise have the restore succeed having pinned nothing that matters.
#
# Known limitation: a PR that legitimately edits .claude/ or CLAUDE.md will have
# those edits reverted for the duration of this run. Same tradeoff
# claude-code-action makes — narrow UX cost for closing the RCE surface.
#
# Runs before the credential-isolation handoff: it needs the git credential
# actions/checkout persisted, which setup-sandbox.sh strips.
#
# Inputs (env): GITHUB_TOKEN (for gh), GITHUB_EVENT_NAME, GITHUB_EVENT_PATH
# (from Actions).
set -eo pipefail

SENSITIVE=(.claude .mcp.json .claude.json .gitmodules .ripgreprc CLAUDE.md CLAUDE.local.md AGENTS.md .husky)

case "$GITHUB_EVENT_NAME" in
  pull_request_target|pull_request_review|pull_request_review_comment)
    BASE_REF=$(jq -r '.pull_request.base.ref' "$GITHUB_EVENT_PATH")
    ;;
  issue_comment)
    PR_URL=$(jq -r '.issue.pull_request.url // empty' "$GITHUB_EVENT_PATH")
    if [ -z "$PR_URL" ]; then
      echo "issue_comment on issue (not PR); nothing to restore"
      exit 0
    fi
    BASE_REF=$(gh api "${PR_URL#https://api.github.com/}" --jq '.base.ref')
    ;;
  *)
    echo "Event $GITHUB_EVENT_NAME is not a PR event; nothing to restore"
    exit 0
    ;;
esac

if [ -z "$BASE_REF" ] || [ "$BASE_REF" = "null" ]; then
  echo "::warning::Could not determine base ref; skipping config restoration"
  exit 0
fi

echo "Restoring ${SENSITIVE[*]} from origin/$BASE_REF"

# Snapshot PR-authored versions to .claude-pr/ for optional review
rm -rf .claude-pr
for p in "${SENSITIVE[@]}"; do
  if [ -e "$p" ]; then
    mkdir -p ".claude-pr/$(dirname "$p")"
    cp -aL "$p" ".claude-pr/$p" 2>/dev/null || true
  fi
done
if [ -d .claude-pr ]; then
  EXCLUDE_FILE="$(git rev-parse --git-path info/exclude)"
  mkdir -p "$(dirname "$EXCLUDE_FILE")"
  if ! grep -qxF '/.claude-pr/' "$EXCLUDE_FILE" 2>/dev/null; then
    [ -s "$EXCLUDE_FILE" ] && [ "$(tail -c1 "$EXCLUDE_FILE" | wc -l)" -eq 0 ] && echo "" >> "$EXCLUDE_FILE"
    echo '/.claude-pr/' >> "$EXCLUDE_FILE"
  fi
fi

# Delete BEFORE fetch so attacker-controlled .gitmodules can't stall on
# credential prompts (git's default fetch.recurseSubmodules=on-demand).
for p in "${SENSITIVE[@]}"; do
  rm -rf "$p"
done

git fetch origin "$BASE_REF" --depth=1 --no-recurse-submodules

for p in "${SENSITIVE[@]}"; do
  git checkout "origin/$BASE_REF" -- "$p" 2>/dev/null || true
done

# The list above is root-anchored, but nested instruction files are trusted
# input too: Claude Code loads the instruction file nearest the file the agent
# opens, so a fork's `site/CLAUDE.md` reaches the session with the root one
# already restored. A nested `.claude/` is the same channel by another route —
# Claude Code discovers directory-scoped skills, so `apps/web/.claude/skills/
# <name>/SKILL.md` is loaded for work under `apps/web/`, and a skill's
# `description` enters the system prompt whether or not the agent ever invokes
# it. That needs no nested `.claude/` on the base branch to exploit: a fork can
# add the first one.
#
# Enumerated from the base tree after the fetch rather than named in SENSITIVE —
# the base tree is what says which paths are legitimate, and only the root
# entries have to be gone *before* the fetch (those are the ones git itself
# reads). Reconciled file-by-file rather than replaced wholesale the way root
# `.claude/` is: an `rm -rf apps/web/.claude` writes through a fork-planted
# `apps/web` symlink, which the two-pass split below avoids by construction.
NESTED=()
while IFS= read -r -d '' rel; do
  case "$rel" in
    */CLAUDE.md | */CLAUDE.local.md | */AGENTS.md | */.claude/*) NESTED+=("$rel") ;;
  esac
done < <(git ls-tree -rz --name-only "origin/$BASE_REF")

# Fork-added ones have no base version to restore, so drop them outright.
# `find` does not descend symlinks, so every path here is inside the checkout
# and `rm` unlinks a fork's symlink rather than its target. The base-tree paths
# are left to `git checkout`, which replaces a path without writing through a
# fork-planted symlink — an `rm -rf docs/CLAUDE.md` would follow a `docs`
# symlink straight out of the worktree. `.claude-pr/` is excluded because it is
# the snapshot this script just wrote: its entries have no base counterpart by
# construction, so the sweep would delete the very copies review skills read.
#
# The nested `.claude/` clause takes files and symlinks but not directories:
# unlinking a leaf never leaves `find` descending into a path it already
# removed, and a skill directory stripped of its `SKILL.md` is not a skill. The
# root `.claude/` is inside this glob too (`./.claude/…` contains `/.claude/`),
# but it was replaced wholesale before the fetch, so everything under it now
# has a base counterpart and the sweep is a no-op there.
while IFS= read -r -d '' path; do
  rel="${path#./}"
  git cat-file -e "origin/$BASE_REF:$rel" 2>/dev/null || rm -rf -- "$path"
done < <(find . \( -name CLAUDE.md -o -name CLAUDE.local.md -o -name AGENTS.md -o \( -path '*/.claude/*' ! -type d \) \) -not -path './.git/*' -not -path './.claude-pr/*' -print0)

if [ ${#NESTED[@]} -gt 0 ]; then
  for p in "${NESTED[@]}"; do
    git checkout "origin/$BASE_REF" -- "$p" 2>/dev/null || true
  done
  echo "Pinned ${#NESTED[@]} nested instruction file(s) to origin/$BASE_REF"
fi

# Unstage — `git checkout <ref> -- <path>` stages restored files.
git reset -- "${SENSITIVE[@]}" "${NESTED[@]}" 2>/dev/null || true

echo "Restored from origin/$BASE_REF"
