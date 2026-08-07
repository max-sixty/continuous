"""Shared test constants and helpers."""

from __future__ import annotations

from importlib.metadata import version

# The generator pins the action ref to its own release version
# (tend.workflows._action_ref). Tests derive the expected ref the same way so
# version bumps don't churn assertions.
ACTION_VERSION = version("tend")
