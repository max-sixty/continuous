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
  rm -f -- CLAUDE.md
  echo "No CLAUDE.md on ${DEFAULT_BRANCH} — removed fork version"
fi

# Remove AGENTS.md files introduced by the fork.
find . -name AGENTS.md -not -path './.git/*' -print0 |
  while IFS= read -r -d '' path; do
    rel="${path#./}"
    if ! git cat-file -e "${BASE_REF}:${rel}" 2>/dev/null; then
      rm -f -- "$path"
      echo "No ${rel} on ${DEFAULT_BRANCH} — removed fork version"
    fi
  done

# Restore every base-branch AGENTS.md, including files deleted by the fork.
while IFS= read -r -d '' rel; do
  case "$rel" in
    AGENTS.md | */AGENTS.md)
      mkdir -p -- "$(dirname "$rel")"
      git restore --source="$BASE_REF" --worktree -- "$rel"
      echo "Pinned ${rel} to ${DEFAULT_BRANCH}"
      ;;
  esac
done < <(git -c core.quotepath=off ls-tree -rz --name-only "$BASE_REF")
