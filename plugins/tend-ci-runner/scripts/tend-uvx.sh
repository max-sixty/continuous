#!/usr/bin/env bash
# Runs `uvx` from a tend-private uv, installing it on first use. Arguments pass
# straight through: `tend-uvx.sh tend@latest init` is `uvx tend@latest init`.
#
# tend ships as a uv tool, but the harness provisions no uv for the agent. One
# installed at the default $HOME/.local/bin would sit ahead of the repo's own
# toolchain on the agent's PATH and override the version the project pins — so
# the uv that runs tend lives in its own directory and never joins PATH. The
# repo's uv, wherever its `setup:` put it, stays the one every other command
# resolves.
#
# Idempotent, so each skill step can call it without tracking whether an
# earlier one already did (shell state doesn't survive between the agent's
# bash calls).
set -euo pipefail

UV_DIR="$HOME/.tend-uv"

if [ ! -x "${UV_DIR}/uvx" ]; then
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh -s -- --quiet
fi

exec "${UV_DIR}/uvx" "$@"
