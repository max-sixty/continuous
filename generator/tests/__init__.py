"""Shared test constants and helpers."""

from __future__ import annotations

from importlib.metadata import version

from tend.config import Config
from tend.workflows import GeneratedWorkflow, generate_all

# The generator pins the action ref to its own release version
# (tend.workflows._action_ref). Tests derive the expected ref the same way so
# version bumps don't churn assertions.
ACTION_VERSION = version("tend")


def agent_workflows(cfg: Config) -> list[GeneratedWorkflow]:
    """The generated workflows that actually invoke a harness.

    Not every workflow runs an agent: tend-mention-relay exists to convert the
    review events into a `repository_dispatch` and deliberately holds neither a
    harness nor a secret. Assertions about harness pinning and model credentials
    apply to the ones that do, so they filter through here rather than naming
    the exception each time.
    """
    # Keyed on the `uses:` action ref, not a bare repo slug — the generated
    # header links to max-sixty/tend/issues, which every workflow carries.
    agentic = [wf for wf in generate_all(cfg) if "uses: max-sixty/tend/" in wf.content]
    assert agentic, "no agent-bearing workflows generated — assertion would be vacuous"
    return agentic
