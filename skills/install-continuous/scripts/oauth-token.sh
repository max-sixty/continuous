#!/usr/bin/env bash
# OAuth 2.0 PKCE flow for Claude Code — prints an access token to stdout.
# Opens a browser for the user to sign in with their Claude account.
# Token is valid for 1 year (31536000 seconds).
#
# Usage: TOKEN=$(./oauth-token.sh)
set -euo pipefail

CLIENT_ID="9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL="https://claude.ai/oauth/authorize"
TOKEN_URL="https://console.anthropic.com/v1/oauth/token"
REDIRECT_PORT=54545
REDIRECT_URI="http://localhost:${REDIRECT_PORT}/callback"
SCOPE="user:inference"
EXPIRES_IN=31536000

# URL-safe base64: replace +/ with -_, strip =
url_base64() { openssl base64 | tr -d '\n=' | tr '+/' '-_'; }

# PKCE: generate code_verifier and code_challenge
CODE_VERIFIER=$(openssl rand 32 | url_base64)
CODE_CHALLENGE=$(printf '%s' "$CODE_VERIFIER" | openssl dgst -sha256 -binary | url_base64)
STATE=$(openssl rand 32 | url_base64)

# Build authorize URL
ENCODED_REDIRECT=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$REDIRECT_URI'))")
AUTH_URL="${AUTHORIZE_URL}?code=true&client_id=${CLIENT_ID}&response_type=code&redirect_uri=${ENCODED_REDIRECT}&scope=${SCOPE}&code_challenge=${CODE_CHALLENGE}&code_challenge_method=S256&state=${STATE}"

# Start a temporary HTTP server to catch the callback
FIFO=$(mktemp -u)
mkfifo "$FIFO"
cleanup() { rm -f "$FIFO"; kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

# Listen for the OAuth callback in the background.
# Use python3 for the HTTP server — portable and already required for URL encoding.
python3 -c "
import http.server, urllib.parse, sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        code = params.get('code', [''])[0]
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<h2>Authentication successful</h2><p>You can close this tab.</p>')
        # Write code to FIFO, then shut down
        with open(sys.argv[1], 'w') as f:
            f.write(code)
        raise SystemExit(0)
    def log_message(self, *args):
        pass  # suppress request logging

srv = http.server.HTTPServer(('127.0.0.1', int(sys.argv[2])), Handler)
srv.handle_request()
" "$FIFO" "$REDIRECT_PORT" &
SERVER_PID=$!

# Open browser
>&2 echo "Opening browser for Claude authentication..."
if command -v open &>/dev/null; then
  open "$AUTH_URL"
elif command -v xdg-open &>/dev/null; then
  xdg-open "$AUTH_URL"
else
  >&2 echo "Open this URL in your browser:"
  >&2 echo "$AUTH_URL"
fi

>&2 echo "Waiting for authentication..."
AUTH_CODE=$(cat "$FIFO")

if [ -z "$AUTH_CODE" ]; then
  >&2 echo "Error: No authorization code received"
  exit 1
fi

# Exchange authorization code for access token
RESPONSE=$(curl -s -X POST "$TOKEN_URL" \
  -H "Content-Type: application/json" \
  -d "{
    \"grant_type\": \"authorization_code\",
    \"code\": \"${AUTH_CODE}\",
    \"redirect_uri\": \"${REDIRECT_URI}\",
    \"client_id\": \"${CLIENT_ID}\",
    \"code_verifier\": \"${CODE_VERIFIER}\",
    \"state\": \"${STATE}\",
    \"expires_in\": ${EXPIRES_IN}
  }")

ACCESS_TOKEN=$(echo "$RESPONSE" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null || true)

if [ -z "$ACCESS_TOKEN" ]; then
  >&2 echo "Error: Failed to exchange code for token"
  >&2 echo "$RESPONSE"
  exit 1
fi

>&2 echo "Authentication successful."
echo "$ACCESS_TOKEN"
