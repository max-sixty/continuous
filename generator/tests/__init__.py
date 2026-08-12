"""Shared test constants and helpers."""

from __future__ import annotations

import shutil
from importlib.metadata import version

# Interpreter for the repo's shell scripts. A bare `bash` would resolve through
# the PATH each test sets for its fake binaries, reaching macOS's /bin/bash 3.2
# — which lacks builtins the runner's bash 5 has (`mapfile`).
BASH = shutil.which("bash")

# The generator pins the action ref to its own release version
# (tend.workflows._action_ref). Tests derive the expected ref the same way so
# version bumps don't churn assertions.
ACTION_VERSION = version("tend")
