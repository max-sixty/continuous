#!/usr/bin/env bash
# Install pinned uv into a caller-owned directory without changing PATH.
# The Claude proxy uses an absolute path to its copy. Agent setup places a
# separate copy after adopter-provided PATH entries.
#
# Inputs (env): UV_VERSION, UV_INSTALL_DIR.
set -euo pipefail

url="https://astral.sh/uv/${UV_VERSION}/install.sh"
curl -LsSf "$url" \
  | env UV_UNMANAGED_INSTALL="$UV_INSTALL_DIR" sh -s -- --quiet

"${UV_INSTALL_DIR}/uv" --version
"${UV_INSTALL_DIR}/uvx" --version
