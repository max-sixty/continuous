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
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from ruamel.yaml import YAML
from tend.config import Config
from tend.workflows import GENERATORS, generate_all

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

GHA_EXPR = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
ACTIONLINT_SHELLCHECK_REV = "v1.7.12"
ACTIONLINT_SHELLCHECK_EXCLUDES = "SC1091,SC2194,SC2050,SC2153,SC2154,SC2157,SC2043"
ACTIONLINT_SHELLCHECK_ARGS = (
    "--norc",
    "-x",
    "--shell",
    "bash",
    "-e",
    ACTIONLINT_SHELLCHECK_EXCLUDES,
)


def _prepare_composite_body(body: str) -> str:
    """Replace expressions with the opaque variable used by this local check."""
    return GHA_EXPR.sub("${_GHA_EXPR}", body)


def _prepare_workflow_body(body: str) -> str:
    """Apply actionlint's expression substitution and Bash setup preamble."""
    sanitized = GHA_EXPR.sub(
        lambda match: "_" * len(match.group().encode("utf-8")), body
    )
    return f"set -eo pipefail\n{sanitized}\n"


def _shellcheck_bodies(
    shellcheck: str,
    args: Sequence[str],
    bodies: list[tuple[str, str]],
    prepare: Callable[[str], str],
) -> None:
    findings = []
    for label, body in bodies:
        result = subprocess.run(
            [shellcheck, *args, "-"],
            input=prepare(body),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            findings.append(f"--- {label}\n{result.stdout}")

    assert not findings, "\n".join(findings)


def test_actionlint_shellcheck_contract_tracks_pin() -> None:
    """Make actionlint upgrades refresh the emulated ShellCheck contract."""
    config = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text()
    )
    actionlint = next(
        repo for repo in config["repos"] if repo["repo"].endswith("/actionlint")
    )
    assert actionlint["rev"] == ACTIONLINT_SHELLCHECK_REV


def test_generated_run_bodies_pass_shellcheck(tmp_path: Path) -> None:
    """Lint the in-tree generator output at actionlint's ShellCheck severity."""
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "install shellcheck (preinstalled on CI runners)"

    config = tmp_path / ".config" / "tend.yaml"
    config.parent.mkdir()
    config.write_text(
        "bot_name: test-bot\n"
        "setup:\n"
        "  - uses: ./.github/actions/tend-setup\n"
        "workflows:\n"
        "  ci-fix:\n"
        '    watched_workflows: ["ci"]\n'
    )
    workflows = generate_all(Config.load(config), with_install_test=True)
    assert {workflow.filename for workflow in workflows} == {
        f"tend-{name}.yaml" for name in GENERATORS
    } | {"tend-install-test.yaml"}

    bodies = []
    for workflow in workflows:
        document = YAML(typ="safe", pure=True).load(workflow.content)
        jobs = document["jobs"]
        for job_name, job in jobs.items():
            steps = [step for step in job.get("steps", []) if "run" in step]
            if not steps:
                continue
            assert "defaults" not in document, f"{workflow.filename}: non-default shell"
            runner = job["runs-on"]
            assert isinstance(runner, str) and runner.startswith("ubuntu-"), (
                f"{workflow.filename} :: {job_name}: non-Bash runner"
            )
            assert "defaults" not in job, (
                f"{workflow.filename} :: {job_name}: non-default shell"
            )
            for step in steps:
                label = (
                    f"{workflow.filename} :: {job_name} :: "
                    f"{step.get('name', '<unnamed>')}"
                )
                assert "shell" not in step, f"{label}: non-default shell"
                bodies.append((label, step["run"]))

    assert bodies, "generated workflows contain no `run:` bodies"
    _shellcheck_bodies(
        shellcheck,
        ACTIONLINT_SHELLCHECK_ARGS,
        bodies,
        _prepare_workflow_body,
    )


def test_actionlint_shellcheck_preprocessing_regression() -> None:
    """Keep the emulation aligned on expression handling and exclusions."""
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "install shellcheck (preinstalled on CI runners)"
    _shellcheck_bodies(
        shellcheck,
        ACTIONLINT_SHELLCHECK_ARGS,
        [
            ("quoted expression", "printf '%s\\n' '${{ github.repository }}'"),
            ("runner-only source", "source ./runner-generated.sh"),
        ],
        _prepare_workflow_body,
    )
    with pytest.raises(AssertionError, match="SC2078"):
        _shellcheck_bodies(
            shellcheck,
            ACTIONLINT_SHELLCHECK_ARGS,
            [
                (
                    "expression truthiness",
                    'if [[ "${{ github.ref }}" ]]; then echo yes; fi',
                )
            ],
            _prepare_workflow_body,
        )
    with pytest.raises(AssertionError, match="SC10"):
        _shellcheck_bodies(
            shellcheck,
            ACTIONLINT_SHELLCHECK_ARGS,
            [
                (
                    "Unicode expression byte length",
                    (
                        "cat <<'${{ true && 'x' || 'é' }}'\n"
                        "body\n"
                        "${{ true && 'x' || 'y' }}"
                    ),
                )
            ],
            _prepare_workflow_body,
        )


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

    bodies = [
        (f"{action} :: {step.get('name', '<unnamed>')}", step["run"]) for step in steps
    ]
    _shellcheck_bodies(
        shellcheck,
        ["-S", "warning", "-s", "bash"],
        bodies,
        _prepare_composite_body,
    )
