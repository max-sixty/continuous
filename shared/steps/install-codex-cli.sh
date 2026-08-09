#!/usr/bin/env bash
# Install the pinned Codex CLI from npm.
#
# The npm registry is the only third-party reach in the codex action, and this
# step runs ahead of `Run Codex` — so a registry blip that isn't ridden out
# costs the whole run, which is the failure lib/retry.sh's window exists for.
#
# `@openai/codex` ships a prebuilt per-platform binary: two packages, no
# dependency tree to resolve and no Rust toolchain to run. Measured on a
# GitHub-hosted runner with an empty npm cache, 2026-08-09: 2.6-3.1 s across
# three installs, which sits far enough inside retry_install's 60 s timeout
# that npm shares the lib's window rather than needing one of its own.
#
# Inputs (env): CODEX_VERSION.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/retry.sh
. "${SCRIPT_DIR}/lib/retry.sh"

retry_install "codex ${CODEX_VERSION}" \
  "npm install -g '@openai/codex@${CODEX_VERSION}'"

codex --version
