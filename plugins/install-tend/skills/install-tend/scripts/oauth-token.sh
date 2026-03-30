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

# claude setup-token is a TUI that starts a localhost server for the OAuth PKCE
# callback. It requires a real TTY — without one the server doesn't bind and the
# browser gets "can't connect to localhost". Claude Code's Bash tool has no TTY.
#
# Fix: script(1) creates a PTY for the child, but fails with "tcgetattr:
# Operation not supported on socket" when its own stdin is a socket (as in
# Claude Code). Redirecting stdin from /dev/null avoids this — the child
# still gets a real PTY.
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

>&2 echo "Running claude setup-token (approve in browser)..."
if [[ "$(uname)" == "Darwin" ]]; then
  script -q "$TMPFILE" claude setup-token < /dev/null
else
  script -qec "claude setup-token" "$TMPFILE" < /dev/null
fi

# Extract the token (sk-ant-oat01-...) from the captured output.
# script(1) captures raw terminal output — ANSI codes and line wraps at the PTY
# width (default 80 cols) can split the token across lines. Strip escape codes
# and newlines so the full token is one continuous string for grep.
TOKEN=$(sed $'s/\033\\[[^a-zA-Z]*[a-zA-Z]//g' "$TMPFILE" | tr -d '\r\n' \
  | grep -o 'sk-ant-oat01-[A-Za-z0-9_-]*' | head -1)

if [ -z "$TOKEN" ]; then
  >&2 echo "Error: Could not extract token from output"
  >&2 cat "$TMPFILE"
  exit 1
fi

>&2 echo "Authentication successful."
echo "$TOKEN"
