#!/usr/bin/env bash
set -euo pipefail

if [[ "$GITHUB_EVENT_NAME" != "pull_request_target" ]] ||
  [[ "$(jq -r '.pull_request.head.repo.fork // false' "$GITHUB_EVENT_PATH")" != true ]]; then
  exit 0
fi

DEFAULT_BRANCH=$(jq -r '.repository.default_branch' "$GITHUB_EVENT_PATH")
BASE_REF="origin/${DEFAULT_BRANCH}"

# Git replaces the path without following a fork-controlled symlink and
# preserves a trusted base-branch symlink as a symlink.
if git cat-file -e "${BASE_REF}:CLAUDE.md" 2>/dev/null; then
  git restore --source="$BASE_REF" --worktree -- CLAUDE.md
  echo "Pinned CLAUDE.md to ${DEFAULT_BRANCH}"
else
  rm -rf -- CLAUDE.md
  echo "No CLAUDE.md on ${DEFAULT_BRANCH} — removed fork version"
fi

# Remove AGENTS.md files introduced by the fork.
find . -name AGENTS.md -not -path './.git/*' -print0 |
  while IFS= read -r -d '' path; do
    rel="${path#./}"
    if ! git cat-file -e "${BASE_REF}:${rel}" 2>/dev/null; then
      rm -rf -- "$path"
      echo "No ${rel} on ${DEFAULT_BRANCH} — removed fork version"
    fi
  done

# Restore every base-branch AGENTS.md, including files deleted by the fork.
while IFS= read -r -d '' rel; do
  case "$rel" in
    AGENTS.md | */AGENTS.md)
      git restore --source="$BASE_REF" --worktree -- "$rel"
      echo "Pinned ${rel} to ${DEFAULT_BRANCH}"
      ;;
  esac
done < <(git ls-tree -rz --name-only "$BASE_REF")

# `.claude/` is instruction input too: `codex/agents-tail.md` tells the agent
# that repo-local skills live at `.claude/skills/<name>/SKILL.md` and that
# `running-in-ci` will send it there, so a fork's copy is read as trusted repo
# guidance exactly the way CLAUDE.md is. The Claude harness already restores
# the directory (`.claude` is in restore-sensitive-config.sh's list); without
# this the Codex harness reviews a fork PR with the fork's skills loaded.
#
# Replaced wholesale rather than reconciled file-by-file: the removal drops
# fork-added skills and any fork symlink standing in for a base directory in
# one step, and the restore then rebuilds the base tree as real files. The
# fork's version stays readable at `git show HEAD:<path>` for a review that
# wants to see what the PR changed — it is only kept out of the worktree the
# agent reads.
mapfile -t -d '' BASE_CLAUDE_FILES < <(git ls-tree -rz --name-only "$BASE_REF" -- .claude)
rm -rf -- .claude
if [ "${#BASE_CLAUDE_FILES[@]}" -gt 0 ]; then
  git restore --source="$BASE_REF" --worktree -- "${BASE_CLAUDE_FILES[@]}"
  echo "Pinned .claude/ to ${DEFAULT_BRANCH} (${#BASE_CLAUDE_FILES[@]} files)"
else
  echo "No .claude/ on ${DEFAULT_BRANCH} — removed fork version"
fi
