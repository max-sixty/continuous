#!/usr/bin/env bash
# Reports bot PAT scope coverage against tend's required classic OAuth scopes.
#
# Classic PATs expose their granted scopes via the X-OAuth-Scopes response
# header. Fine-grained PATs do not — no documented GitHub endpoint reveals
# their permission set — so those are reported as STATUS=fine-grained and
# the caller should skip.
#
# Output (stdout): key=value lines the caller parses.
#   STATUS=ok | missing | fine-grained
#   GRANTED=<csv of granted scopes>    (ok, missing)
#   REQUIRED=<csv of required scopes>  (ok, missing)
#   MISSING=<csv of missing scopes>    (missing)
#
# Required scopes are duplicated in prose at docs/tend.example.toml,
# DESIGN.md, plugins/install-tend/skills/install-tend/SKILL.md, and
# plugins/tend-ci-runner/skills/nightly/SKILL.md — keep in sync when adding
# a scope. This script is the one executable reference.
#
# Exit code: 0 when the check ran to completion (STATUS carries the result);
# non-zero only if gh or bash itself failed.
#
# Requires: gh (authenticated as the bot)

set -euo pipefail

REQUIRED="repo workflow notifications write:discussion gist user"

HEADERS=$(gh api -i user)
SCOPES_LINE=$(printf '%s\n' "$HEADERS" | grep -i '^x-oauth-scopes:' | head -1 || true)

if [ -z "$SCOPES_LINE" ]; then
  echo "STATUS=fine-grained"
  exit 0
fi

GRANTED=$(printf '%s' "$SCOPES_LINE" \
  | sed 's/^[^:]*:[[:space:]]*//' \
  | tr -d '\r' \
  | tr ',' ' ' \
  | xargs)

# Exact match only. install-tend pre-fills the required scopes verbatim, so
# parent-scope equivalence (e.g. admin:discussion satisfying write:discussion)
# is not a common case and is not handled here.
MISSING=""
for req in $REQUIRED; do
  found=0
  for g in $GRANTED; do
    [ "$g" = "$req" ] && found=1 && break
  done
  [ "$found" = "0" ] && MISSING="$MISSING $req"
done
MISSING=$(echo "$MISSING" | xargs || true)

GRANTED_CSV=$(echo "$GRANTED" | tr ' ' ',')
REQUIRED_CSV=$(echo "$REQUIRED" | tr ' ' ',')
MISSING_CSV=$(echo "$MISSING" | tr ' ' ',')

if [ -z "$MISSING" ]; then
  echo "STATUS=ok"
else
  echo "STATUS=missing"
fi
echo "GRANTED=$GRANTED_CSV"
echo "REQUIRED=$REQUIRED_CSV"
echo "MISSING=$MISSING_CSV"
