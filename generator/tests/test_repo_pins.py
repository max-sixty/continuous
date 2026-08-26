"""Cross-file pin invariants that no single suite owns.

`test_pinned_mitmproxy_matches_the_action` (proxy/) is the sibling of this
idea: a version named in two places drifts silently unless something asserts
the pair.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_uv_build_range_admits_the_pinned_uv() -> None:
    # uv only *warns* when `build-system.requires` doesn't contain the uv
    # running the build, so a stale range survives every release and every
    # `uv sync` without failing anything. `uv_version` in claude/action.yaml is
    # the repo's statement of which uv is current — the weekly sweep moves it to
    # the latest release — so tying the range to it makes that sweep carry the
    # backend along instead of leaving it for someone to notice in the noise.
    # Both operands are in-repo, so this can only go red on a bump, never on
    # the day astral publishes something.
    requires = tomllib.loads((REPO_ROOT / "generator" / "pyproject.toml").read_text())[
        "build-system"
    ]["requires"]
    backends = [
        Requirement(r)
        for r in requires
        if canonicalize_name(Requirement(r).name) == "uv-build"
    ]
    assert len(backends) == 1, f"expected one uv_build requirement, got: {requires}"

    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "claude" / "action.yaml").read_text()
    )
    uv_version = action["inputs"]["uv_version"]["default"]

    assert Version(uv_version) in backends[0].specifier, (
        f"build-system.requires pins `{backends[0]}`, which does not contain the "
        f"uv this repo pins ({uv_version}); `uv build` warns and the wheel is "
        "built by a backend a release older than the uv building it"
    )


# Every `${{ github.action_path }}/…` reference in the two composite actions.
# Nothing else reads them: the pre-commit actionlint hook is pinned to
# ^.github/workflows/, so neither action.yaml is linted at all, and no workflow
# here consumes the actions with `uses: ./` — they pin a released ref, so an
# edited body first runs in an adopter's job. A path that resolves nowhere fails
# its step, for every adopter, on the first run after a release.
ACTION_PATH_REF = re.compile(r"\$\{\{\s*github\.action_path\s*\}\}/?([^\s\"')]*)")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_action_path_references_resolve(harness: str) -> None:
    body = (REPO_ROOT / harness / "action.yaml").read_text()
    matched = ACTION_PATH_REF.findall(body)

    assert matched, f"{harness}/action.yaml: no github.action_path references"
    missing = [
        ref
        for ref in sorted(set(matched))
        if not (REPO_ROOT / harness / ref).resolve().exists()
    ]
    assert not missing, f"{harness}/action.yaml references nothing at: {missing}"


# The `sudo -u "$SANDBOX" env` line each adopter-facing crossing builds. Nothing
# else covers claude/action.yaml's: its inline block is unlinted (the actionlint
# hook is pinned to ^.github/workflows/), no workflow consumes the action with
# `uses: ./`, and the test-sandbox job drives shared/steps/sandbox-setup.sh
# directly without going through the action. Deleting the splat there launches
# the agent with no PATH, no proxy routing, no CA trust and no credentials.
CROSSINGS = ("claude/action.yaml", "shared/steps/sandbox-setup.sh")
GITHUB_ASSIGNMENT = re.compile(r"\bGITHUB_[A-Z_]*=")


def _sudo_env_command(body: str, path: str) -> str:
    """The one `sudo -u "$SANDBOX" env …` command, continuations included."""
    lines = body.splitlines()
    starts = [i for i, line in enumerate(lines) if 'sudo -u "$SANDBOX" env' in line]
    assert len(starts) == 1, f"{path}: expected one sudo env crossing, got {starts}"
    i = starts[0]
    command = [lines[i]]
    while lines[i].rstrip().endswith("\\"):
        i += 1
        command.append(lines[i])
    return "\n".join(command)


@pytest.mark.parametrize("crossing", CROSSINGS)
def test_the_crossing_launches_from_the_composed_env(crossing: str) -> None:
    """Something fills the array, it is on the line, and nothing GITHUB_* follows.

    sandbox_launch_env puts the context after the agent env file, so an
    adopter's `sandbox_env:` cannot decide what the run thinks it is. A caller
    is free to append names of its own — tend's BOT_*/TEND_* assignments do,
    and have to, since they must beat the file — but a GITHUB_*-named one would
    land after the context and displace it. Scoped to the single command rather
    than to file order, so it says "later in this argv", which is the thing that
    decides who wins.

    The call is asserted separately because bash expands `"${arr[@]}"` on an
    UNSET array to nothing and exits 0, `set -u` included, so a splat with
    nothing filling it is not an error the shell reports: the crossing would
    launch with an empty environment — no PATH, no proxy routing, no CA trust,
    no credentials — and only the child's exit code would say so.
    """
    body = (REPO_ROOT / crossing).read_text()
    command = _sudo_env_command(body, crossing)

    assert 'sandbox_launch_env "$AGENT_ENV_FILE"' in body, (
        f"{crossing}: nothing composes the launch env, and an unset array "
        f"splats to nothing, so the crossing would launch with an empty one"
    )
    assert '"${SANDBOX_LAUNCH_ENV[@]}"' in command, (
        f"{crossing}: the crossing does not carry the composed launch env"
    )
    trailing = command.split('"${SANDBOX_LAUNCH_ENV[@]}"', 1)[1]
    assert not GITHUB_ASSIGNMENT.search(trailing), (
        f"{crossing}: a GITHUB_* assignment follows the composed array, so it "
        f"displaces the real context: {trailing.strip()}"
    )
