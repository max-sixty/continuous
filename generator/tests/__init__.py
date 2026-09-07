"""Shared test constants and helpers."""

from __future__ import annotations

import shutil
from importlib.metadata import version
from pathlib import Path

from tests._yaml import safe_load


def agent_prompt(content: str) -> str:
    """The `prompt:` input a generated workflow hands the harness action.

    Parsed out of the YAML rather than matched in the text, so a test asserting
    what the agent receives cannot pass on a file GitHub would reject.
    """
    jobs = safe_load(content)["jobs"]
    steps = [step for job in jobs.values() for step in job.get("steps", [])]
    prompts = [s["with"]["prompt"] for s in steps if "prompt" in s.get("with", {})]
    assert len(prompts) == 1, f"expected one agent step, found {len(prompts)}"
    return prompts[0]


# Interpreter for the repo's shell scripts. A bare `bash` would resolve through
# the PATH each test sets for its fake binaries, reaching macOS's /bin/bash 3.2
# — which lacks builtins the runner's bash 5 has (`mapfile`).
BASH = shutil.which("bash")
assert BASH, "bash is required for these tests"

UV = shutil.which("uv")
assert UV, "uv is required for these tests"


def uv_script(path: Path, *args: str) -> list[str]:
    """Command for the same PEP 723 script surface production invokes."""
    return [UV, "run", "--script", str(path), *args]


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


def fake_bin(tmp_path: Path, **scripts: str) -> Path:
    """Write executable command stand-ins (gh, date, …); return the PATH dir."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in scripts.items():
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)
    return bindir


# Shared opening for `gh` stand-ins: log the call, extract a --jq argument,
# and define emit() to run it through real jq — the script's own filter is
# usually the behaviour under test, so a fake that pre-filtered would assert
# nothing. `-c` matches `gh --jq`, which writes one line per result: a filter
# constructing objects (`{body, in_reply_to_id}`) depends on that, and jq's
# default pretty-printing would hand the script a shape gh never produces.
# Fakes append their own `case` dispatch: GH_PREAMBLE + r'''case …'''.
GH_PREAMBLE = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_CALLS"

jq_expr=""
prev=""
for arg in "$@"; do
  [ "$prev" = "--jq" ] && jq_expr="$arg"
  prev="$arg"
done

emit() {
  if [ -n "$jq_expr" ]; then
    printf '%s' "$1" | jq -rc "$jq_expr"
  else
    printf '%s' "$1"
  fi
}
"""
