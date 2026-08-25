"""Cross-file pin invariants that no single suite owns.

`test_pinned_mitmproxy_matches_the_action` (proxy/) is the sibling of this
idea: a version named in two places drifts silently unless something asserts
the pair.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from ruamel.yaml import YAML
from tests import _yaml as yaml

from tend.config import Config
from tend.workflows import generate_all

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


def test_poll_exemptions_match_the_agentless_generated_workflows(
    tmp_path: Path,
) -> None:
    # poll-pr-checks.sh exempts every generated `tend-*` workflow from gating
    # because none of them is a verdict on the code — they run agent sessions,
    # whose red is a tend outage the caller cannot act on. It then names the
    # agentless ones back in by hand, so that rationale holds only while those
    # two sets stay complements: a second deterministic generated workflow
    # (another drift or lint check) would silently inherit the prefix exemption
    # and its red would read as green, with nothing going red to say so. Both
    # operands are in-repo, so this can only fail on a change to one of them.
    cfg_path = tmp_path / ".config" / "tend.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "bot_name: test-bot\nworkflows:\n  ci-fix:\n    watched_workflows: [ci]\n"
    )
    script = (
        REPO_ROOT / "plugins" / "tend-ci-runner" / "scripts" / "poll-pr-checks.sh"
    ).read_text()
    named_back_in = set(re.findall(r'\.workflow == "(tend-[\w-]+)"', script))

    for wf in generate_all(Config.load(cfg_path), with_install_test=True):
        name = yaml.safe_load(wf.content)["name"]
        if not name.startswith("tend-"):
            continue
        runs_agent = "uses: max-sixty/tend/" in wf.content
        assert runs_agent != (name in named_back_in), f"`{name}` " + (
            "runs an agent job but poll-pr-checks.sh names it back in as a "
            "gating check, so a tend outage on it holds up every caller's poll"
            if runs_agent
            else "runs no agent job, so the `tend-` exemption in "
            "poll-pr-checks.sh drops it from the rollup and a red one reads "
            "as green — name it back in alongside `tend-install-test`"
        )
