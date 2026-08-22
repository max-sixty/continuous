#!/usr/bin/env bash
# Shared machinery for pinning a PR's instruction files to the base branch:
# restore-sensitive-config.sh (Claude harness) and pin-instruction-files.sh
# (Codex harness). Sourced, not executed.
#
# Instruction files are trusted repo guidance at any depth, not only the root.
# Claude Code loads the CLAUDE.md, CLAUDE.local.md, or AGENTS.md nearest the
# file the agent opens, and the skills under any directory's .claude/; Codex
# reads the AGENTS.md beside the files it opens, and codex/agents-tail.md
# sends it to .claude/skills/ for repo-local skills. So a fork's `site/CLAUDE.md`
# reaches the session as readily as a root one. The bare `**/.claude` catches a
# fork symlink planted at the `.claude` component itself, which the contents
# glob can't match.
# shellcheck disable=SC2034  # read by the sourcing scripts
INSTRUCTION_PATHSPECS=(':(glob)**/CLAUDE.md' ':(glob)**/CLAUDE.local.md' ':(glob)**/AGENTS.md' ':(glob)**/.claude' ':(glob)**/.claude/**')

# pin_to_base <ref> <pathspec>... — make the worktree match <ref> for every
# path the pathspecs cover. One `git restore --source` call does all of it: it
# writes <ref>'s version of each path, removes the ones <ref> lacks, and
# replaces a fork-planted symlink instead of writing through it. `--worktree`
# leaves the index at HEAD, so the revert doesn't ride into commits the agent
# makes later, and the fork's version stays readable at `git show HEAD:<path>`
# for a review that wants to see what the PR changed. The diff builds the path
# list first because `restore` refuses a pathspec that matches nothing on
# either side. `--no-renames` keeps both ends of a file the fork moved in that
# list; with rename detection the diff would name only the destination.
# `--ignore-submodules=none` keeps a fork's .gitmodules from hiding a gitlink
# it planted at one of these paths. `wait` surfaces the diff's own exit status,
# which the process substitution would otherwise drop: an unresolvable <ref>
# fails the step instead of pinning nothing.
pin_to_base() {
  local ref=$1
  shift
  local -a paths
  mapfile -d '' paths < <(git diff --no-renames --ignore-submodules=none --name-only -z "$ref" -- "$@")
  wait "$!"
  if [ ${#paths[@]} -gt 0 ]; then
    git restore --source="$ref" --worktree -- "${paths[@]}"
    printf '%s\n' "${paths[@]}"
  fi
  echo "Pinned ${#paths[@]} path(s) to $ref"
}
