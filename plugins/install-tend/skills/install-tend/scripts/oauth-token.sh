#!/usr/bin/env bash
# Obtain a long-lived Claude Code OAuth token via `claude setup-token`.
# Opens a browser for authentication, prints the access token to stdout.
#
# Requires: claude CLI
# Usage: TOKEN=$(./oauth-token.sh)
set -euo pipefail

if ! command -v claude &>/dev/null; then
  >&2 echo "Error: claude CLI not found. Install Claude Code first."
  exit 1
fi

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

# claude setup-token is a TUI that starts a localhost server for the OAuth PKCE
# callback. It requires a real TTY — without one the server doesn't bind and the
# browser gets "can't connect to localhost". Claude Code's Bash tool has no TTY.
#
# Strategy: if tmux is available, use it to provide a PTY. Otherwise, tell the
# caller to run the command in their own terminal.
if command -v tmux &>/dev/null; then
  SESSION="oauth-token-$$"
  trap 'rm -f "$TMPFILE"; tmux kill-session -t "$SESSION" 2>/dev/null || true' EXIT

  >&2 echo "Running claude setup-token (approve in browser)..."

  # tmux provides a PTY; macOS script(1) captures raw output to the file.
  tmux new-session -d -s "$SESSION" \
    "script -q '$TMPFILE' claude setup-token; tmux wait-for -S '$SESSION'"
  tmux wait-for "$SESSION"
else
  >&2 echo "Error: tmux not found — claude setup-token needs a TTY."
  >&2 echo "Run this command in your terminal, then paste the token back:"
  >&2 echo ""
  >&2 echo "  claude setup-token"
  >&2 echo ""
  exit 1
fi

# Extract the token (sk-ant-oat01-...) from the captured output
TOKEN=$(grep -o 'sk-ant-oat01-[A-Za-z0-9_-]*' "$TMPFILE" | head -1)

if [ -z "$TOKEN" ]; then
  >&2 echo "Error: Could not extract token from output"
  >&2 cat "$TMPFILE"
  exit 1
fi

>&2 echo "Authentication successful."
echo "$TOKEN"
