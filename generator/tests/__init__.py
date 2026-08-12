"""Shared test constants and helpers."""

from __future__ import annotations

import shutil
from importlib.metadata import version
from pathlib import Path

# Interpreter for the repo's shell scripts. A bare `bash` would resolve through
# the PATH each test sets for its fake binaries, reaching macOS's /bin/bash 3.2
# — which lacks builtins the runner's bash 5 has (`mapfile`).
BASH = shutil.which("bash")
assert BASH, "bash is required for these tests"


def tool_path(*fakes: Path | str) -> str:
    """PATH for a script under test: *fakes* first, then jq, then coreutils.

    The scripts and their fake `gh` both shell out to jq, which the runners
    carry in /usr/bin and a developer's box may not, so it is resolved rather
    than assumed. Resolved per call, so a suite that needs no jq doesn't
    require one.
    """
    jq = shutil.which("jq")
    assert jq, "jq is required for these tests"
    return ":".join(
        [*(str(f) for f in fakes), str(Path(jq).parent), "/usr/bin", "/bin"]
    )


# The generator pins the action ref to its own release version
# (tend.workflows._action_ref). Tests derive the expected ref the same way so
# version bumps don't churn assertions.
ACTION_VERSION = version("tend")
