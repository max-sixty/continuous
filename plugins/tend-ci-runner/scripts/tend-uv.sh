#!/usr/bin/env bash
# Runs a uv command from a tend-private installation, installing it on first
# use. Arguments pass straight through to uv.
#
# The harness provisions no uv for the agent. Installing one into the default
# ~/.local/bin could shadow the adopter's project-pinned toolchain, so this uv
# lives in its own directory and never joins PATH.
set -euo pipefail

UV_VERSION="0.12.7"
UV_DIR="$HOME/.tend-uv/$UV_VERSION"

if [ ! -x "${UV_DIR}/uv" ]; then
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
    | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh -s -- --quiet
fi

exec "${UV_DIR}/uv" "$@"
