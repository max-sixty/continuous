#!/usr/bin/env bash
# Keep the harness's own `.claude/settings.local.json` out of anything the agent
# stages. The Claude action writes that file into the adopter's checkout, where
# it is untracked and unignored — and adopters track `.claude/skills/` (the
# `running-tend` overlay), so a session that stages broadly (`git add -A`) sweeps
# it into the PR it opens: one line of JSON beside a real code change, invisible
# in review. Merged, it makes `permissions.defaultMode: bypassPermissions` the
# repo's checked-in default for everyone who clones it.
#
# `.git/info/exclude` is per-checkout and never committed, so no adopter repo has
# to change, and linked worktrees (which `tend-nightly` creates under /tmp) read
# it from the common git dir.
#
# Runs as the sandbox user, which owns the workspace by this point, with the
# workspace as the working directory. Used by the Claude harness action.
set -euo pipefail

PATTERN='/.claude/settings.local.json'

# No checkout, nothing to exclude. A hard failure here would cost the whole run
# over a guard the agent may not even need.
if ! EXCLUDE_FILE="$(git rev-parse --git-path info/exclude 2>/dev/null)"; then
  echo "[exclude-local-settings] not a git checkout; nothing to exclude"
  exit 0
fi

mkdir -p "$(dirname "$EXCLUDE_FILE")"
if ! grep -qxF "$PATTERN" "$EXCLUDE_FILE" 2>/dev/null; then
  # Terminate a last line written without a newline (the PR-event config
  # restore appends to this same file) so the append lands on its own line.
  if [ -s "$EXCLUDE_FILE" ] && [ "$(tail -c1 "$EXCLUDE_FILE" | wc -l)" -eq 0 ]; then
    echo "" >> "$EXCLUDE_FILE"
  fi
  echo "$PATTERN" >> "$EXCLUDE_FILE"
fi

echo "[exclude-local-settings] $PATTERN excluded via $EXCLUDE_FILE"
