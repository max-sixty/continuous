#!/usr/bin/env bash
# Shared machinery used by restore-sensitive-config.sh to pin a PR's
# instruction files to the base branch under both harnesses. Sourced, not
# executed.
#
# Instruction files are trusted repo guidance at any depth, not only the root.
# Claude Code loads the CLAUDE.md, CLAUDE.local.md, or AGENTS.md nearest the
# file the agent opens, and the skills under any directory's .claude/; Codex
# reads the AGENTS.md beside the files it opens and discovers skills under
# .agents/. So a fork's `site/CLAUDE.md` reaches the session as readily as a
# root one. The bare directory pathspecs catch a fork symlink planted at the
# `.claude` or `.agents` component itself, which the contents globs can't match.
# shellcheck disable=SC2034  # read by the sourcing scripts
INSTRUCTION_PATHSPECS=(':(glob)**/CLAUDE.md' ':(glob)**/CLAUDE.local.md' ':(glob)**/AGENTS.md' ':(glob)**/.claude' ':(glob)**/.claude/**' ':(glob)**/.agents' ':(glob)**/.agents/**')

# pin_to_base <ref> <pathspec>... — make the worktree match <ref> for every
# path the pathspecs cover. One `git restore --source` call does all of it: it
# writes <ref>'s version of each path, removes the ones <ref> lacks, and
# replaces a fork-planted symlink instead of writing through it. When a path is
# a symlink or file on one side and a directory on the other, the diff names
# both that path and its children. Restoring only the topmost changed path lets
# Git replace the path before it considers anything beneath it.
# `--worktree` leaves the index at HEAD, so the revert doesn't ride into commits
# the agent makes later, and the fork's version stays readable at
# `git show HEAD:<path>` for a review that wants to see what the PR changed.
# `--no-renames` keeps both ends of a file the fork moved in the changed-path
# list; with rename detection the diff would name only the destination.
# `--ignore-submodules=none` keeps a fork's .gitmodules from hiding a gitlink
# it planted at one of these paths. `wait` surfaces the diff's own exit status,
# which the process substitution would otherwise drop: an unresolvable <ref>
# fails the step instead of pinning nothing.
pin_to_base() {
  local ref=$1
  shift
  local -a paths
  local -a restore_paths=()
  local path parent covered
  mapfile -d '' paths < <(git diff --no-renames --ignore-submodules=none --name-only -z "$ref" -- "$@")
  wait "$!"
  for path in "${paths[@]}"; do
    covered=false
    for parent in "${paths[@]}"; do
      if [[ "$path" != "$parent" && "$path" == "$parent/"* ]]; then
        covered=true
        break
      fi
    done
    if [[ "$covered" == false ]]; then
      restore_paths+=("$path")
    fi
  done
  if [ ${#paths[@]} -gt 0 ]; then
    git restore --source="$ref" --worktree -- "${restore_paths[@]}"
    printf '%s\n' "${paths[@]}"
  fi
  echo "Pinned ${#paths[@]} path(s) to $ref"
}
