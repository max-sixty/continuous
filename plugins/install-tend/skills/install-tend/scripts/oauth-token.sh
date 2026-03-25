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

# claude setup-token runs the OAuth PKCE flow (opens browser, exchanges token).
# It's a TUI app — redirect to a file (piping breaks it).
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

>&2 echo "Running claude setup-token (approve in browser)..."
claude setup-token > "$TMPFILE" 2>&1

# Extract the token (sk-ant-oat01-...) from the TUI output
TOKEN=$(grep -o 'sk-ant-oat01-[A-Za-z0-9_-]*' "$TMPFILE" | head -1)

if [ -z "$TOKEN" ]; then
  >&2 echo "Error: Could not extract token from output"
  >&2 cat "$TMPFILE"
  exit 1
fi

>&2 echo "Authentication successful."
echo "$TOKEN"
