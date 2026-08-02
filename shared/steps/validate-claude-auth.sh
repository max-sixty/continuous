#!/usr/bin/env bash
# Fail fast unless a Claude credential is configured. Used by the Claude
# harness action (claude/).
#
# Secrets arrive via env (not argv/interpolation) so their values never land in
# a rendered step script on disk.
#
# Inputs (env): CLAUDE_CODE_OAUTH_TOKEN and/or ANTHROPIC_API_KEY.
set -eo pipefail

if [ -z "$CLAUDE_CODE_OAUTH_TOKEN" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "::error::No Claude auth configured. Set either the CLAUDE_CODE_OAUTH_TOKEN secret (subscription-funded, via \`claude setup-token\`) or the ANTHROPIC_API_KEY secret (billed per token via console.anthropic.com)."
  exit 1
fi
