"""Cross-file pin invariants that no single suite owns.

`test_pinned_mitmproxy_matches_the_action` (proxy/) is the sibling of this
idea: a version named in two places drifts silently unless something asserts
the pair.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_claude_transcript_summary_is_opt_in() -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "claude" / "action.yaml").read_text()
    )

    assert action["inputs"]["show_full_output"]["default"] == "false"


def test_experimental_memory_gist_sync_cannot_replace_the_agent_verdict() -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "claude" / "action.yaml").read_text()
    )
    steps = {step["name"]: step for step in action["runs"]["steps"]}

    assert action["inputs"]["memory_gist"]["default"] == "false"
    assert steps["Restore experimental memory Gist"]["continue-on-error"] is True
    assert steps["Save experimental memory Gist"]["continue-on-error"] is True
    assert (
        steps["Remove experimental memory Gist working copy"]["continue-on-error"]
        is True
    )
    assert (
        steps["Save experimental memory Gist"]["if"]
        == "always() && steps.auto_memory.outcome == 'success' && "
        "steps.claude.outputs.sandbox_reaped == 'true'"
    )
    restore = steps["Restore experimental memory Gist"]["run"]
    save = steps["Save experimental memory Gist"]["run"]
    assert 'gist_memory.py" \\\n  restore;' in restore
    assert 'gist_memory.py" \\\n  save;' in save


def test_uv_build_range_admits_the_pinned_uv() -> None:
    # uv only *warns* when `build-system.requires` doesn't contain the uv
    # running the build, so a stale range survives every release and every
    # `uv sync` without failing anything. The harness `uv_version` inputs are
    # the repo's statement of which uv is current — the weekly sweep moves them
    # to the latest release — so tying the range to them makes that sweep carry
    # the backend along instead of leaving it for someone to notice in the noise.
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

    uv_versions = [
        YAML(typ="safe", pure=True).load(
            (REPO_ROOT / harness / "action.yaml").read_text()
        )["inputs"]["uv_version"]["default"]
        for harness in ("claude", "codex")
    ]
    assert uv_versions[0] == uv_versions[1], f"harness uv pins differ: {uv_versions}"
    uv_version = uv_versions[0]

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


# Inline `run:` bodies in the composite actions. Nothing else lints them:
# actionlint only reads workflow files (it parses an action.yaml as a malformed
# workflow — "jobs section is missing"), and the shellcheck hook's `files:`
# regex covers the standalone step scripts, not `claude/` or `codex/`.
ACTIONS = ("claude/action.yaml", "codex/action.yaml")

# actionlint substitutes `${{ … }}` with a shell VARIABLE before handing a body
# to shellcheck, and the choice matters: a literal placeholder makes shellcheck
# judge the surrounding test (`[ -z "literal" ]` → SC2157 "always false",
# `[ "literal" = "true" ]` → SC2050 "expression is constant") and report on the
# substitution rather than the code. A variable is opaque to those checks.
GHA_EXPR = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


@pytest.mark.parametrize("action", ACTIONS)
def test_inline_run_bodies_pass_shellcheck(action: str) -> None:
    """Hold inline action bodies to the standalone-script warning severity."""
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "install shellcheck (preinstalled on CI runners)"

    doc = YAML(typ="safe", pure=True).load((REPO_ROOT / action).read_text())
    steps = [step for step in doc["runs"]["steps"] if "run" in step]
    assert steps, f"{action}: no inline `run:` bodies found — did the schema move?"
    # `-s bash` below is a claim about the body, not a default: a step that
    # pinned `shell: sh` would have its bashisms checked as valid.
    not_bash = [s.get("name") for s in steps if "bash" not in s.get("shell", "")]
    assert not not_bash, f"{action}: not shellcheck-able as bash: {not_bash}"

    findings = []
    for step in steps:
        name, body = step.get("name", "<unnamed>"), step["run"]
        result = subprocess.run(
            [shellcheck, "-S", "warning", "-s", "bash", "-"],
            input=GHA_EXPR.sub("${_GHA_EXPR}", body),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            findings.append(f"--- {action} :: {name}\n{result.stdout}")

    assert not findings, "\n".join(findings)
