#!/usr/bin/env bash
set -euo pipefail

if [[ "$GITHUB_EVENT_NAME" != "pull_request_target" ]] ||
  [[ "$(jq -r '.pull_request.head.repo.fork // false' "$GITHUB_EVENT_PATH")" != true ]]; then
  exit 0
fi

DEFAULT_BRANCH=$(jq -r '.repository.default_branch' "$GITHUB_EVENT_PATH")
BASE_REF="origin/${DEFAULT_BRANCH}"

# Both names are read as trusted repo guidance from whichever directory the
# agent is working in, not only the root: Codex reads the `AGENTS.md` beside
# the files it opens, and Claude Code loads a nested `CLAUDE.md` the same way.
# So the walk covers every directory — a fork's `site/CLAUDE.md` reaches the
# session exactly as a root one would.

# Remove instruction files introduced by the fork.
find . \( -name CLAUDE.md -o -name AGENTS.md \) -not -path './.git/*' -print0 |
  while IFS= read -r -d '' path; do
    rel="${path#./}"
    if ! git cat-file -e "${BASE_REF}:${rel}" 2>/dev/null; then
      rm -rf -- "$path"
      echo "No ${rel} on ${DEFAULT_BRANCH} — removed fork version"
    fi
  done

# Restore every base-branch instruction file, including files deleted by the
# fork. Git replaces the path without following a fork-controlled symlink and
# preserves a trusted base-branch symlink as a symlink.
while IFS= read -r -d '' rel; do
  case "$rel" in
    CLAUDE.md | */CLAUDE.md | AGENTS.md | */AGENTS.md)
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
rm -rf -- .claude
if git cat-file -e "${BASE_REF}:.claude" 2>/dev/null; then
  git restore --source="$BASE_REF" --worktree -- .claude
  echo "Pinned .claude/ to ${DEFAULT_BRANCH}"
else
  echo "No .claude/ on ${DEFAULT_BRANCH} — removed fork version"
fi
