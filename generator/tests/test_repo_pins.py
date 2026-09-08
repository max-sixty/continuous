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
from tend.workflows import UV_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_generated_workflow_snapshot_hook_covers_every_snapshot() -> None:
    config = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text()
    )
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook.get("name") == "Lint generated workflow snapshots"
    ]
    assert len(hooks) == 1, f"expected one snapshot actionlint hook, got: {hooks}"

    snapshots = sorted((REPO_ROOT / "generator/tests/_regtest_outputs").glob("*.out"))
    assert snapshots, "no generated workflow snapshots found"

    pattern = re.compile(hooks[0]["files"])
    unmatched = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in snapshots
        if not pattern.search(path.relative_to(REPO_ROOT).as_posix())
    ]
    assert not unmatched, f"snapshot actionlint hook skips: {unmatched}"


def test_claude_transcript_summary_is_opt_in() -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "claude" / "action.yaml").read_text()
    )

    assert action["inputs"]["show_full_output"]["default"] == "false"


def test_codex_agent_never_receives_the_pat_or_api_key() -> None:
    """Long-lived credentials stop in runner-owned auth and proxy steps."""
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "codex" / "action.yaml").read_text()
    )
    steps = {step["name"]: step for step in action["runs"]["steps"]}

    assert "experimental" in action["name"].lower()
    setup_env = steps["Set up credential-isolation sandbox"]["env"]
    setup_run = steps["Set up credential-isolation sandbox"]["run"]
    assert setup_env["TEND_GH_TOKEN"] == "${{ inputs.github_token }}"
    assert setup_env["TEND_GITHUB_ONLY"] == "1"
    assert "TEND_OPENAI_API_KEY" not in setup_env
    assert 'TEND_GITHUB_ONLY="$TEND_GITHUB_ONLY"' in setup_run
    auth = steps["Configure Codex auth"]
    assert auth["env"]["OPENAI_API_KEY"] == "${{ inputs.openai_api_key }}"
    assert auth["env"]["CODEX_AUTH_JSON"] == "${{ inputs.codex_auth_json }}"
    assert "/usr/bin/env -i" in auth["run"]
    assert "tend-codex-auth.json" in auth["run"]
    openai_proxy = steps["Start OpenAI Responses proxy"]
    assert openai_proxy["if"] == "steps.codex_auth.outputs.mode == 'api-key'"
    assert openai_proxy["env"]["PROXY_API_KEY"] == "${{ inputs.openai_api_key }}"
    assert "exec /usr/bin/env -i" in openai_proxy["run"]
    assert '"$NODE_BIN" "$CODEX_PROXY_BIN"' in openai_proxy["run"]
    assert '<<< "$PROXY_API_KEY"' in openai_proxy["run"]
    assert '> "$PROXY_LOG_FILE" 2>&1 &' in openai_proxy["run"]
    assert 'cat "$PROXY_LOG_FILE" >&2' in openai_proxy["run"]
    assert "OPENAI_API_KEY is unset" in openai_proxy["run"]
    assert [
        name
        for name, step in steps.items()
        if "${{ inputs.openai_api_key }}" in str(step)
    ] == ["Configure Codex auth", "Start OpenAI Responses proxy"]
    assert [
        name
        for name, step in steps.items()
        if "${{ inputs.codex_auth_json }}" in str(step)
    ] == ["Configure Codex auth"]
    subscription = steps["Stage subscription auth (sandbox)"]
    assert subscription["if"] == "steps.codex_auth.outputs.mode == 'subscription'"
    assert subscription["env"] == {
        "BASH_ENV": "",
        "BASHOPTS": "",
        "SHELLOPTS": "",
        "PS4": "",
    }
    assert '"$AGENT_HOME/.codex/auth.json"' in subscription["run"]
    assert 'rm -f -- "$RUNNER_TEMP/tend-codex-auth.json"' in subscription["run"]
    run_env = steps["Run Codex"]["env"]
    assert not (
        {"OPENAI_API_KEY", "CODEX_AUTH_JSON", "GH_TOKEN", "GITHUB_TOKEN"}
        & run_env.keys()
    )
    assert steps["Run Codex"]["run"].endswith('launch_sandbox_runtime.py"')
    assert "CODEX_SANDBOX_MODE" not in run_env
    assert run_env["AUTH_MODE"] == "${{ steps.codex_auth.outputs.mode }}"
    runner = (REPO_ROOT / "codex" / "runner.py").read_text()
    supervisor = (REPO_ROOT / "shared/steps/launch_sandbox_runtime.py").read_text()
    assert "_sandbox.launch_env" in supervisor
    assert 'model_provider="tend-openai"' in runner
    assert steps["Token usage"]["env"]["SANDBOX_REAPED"] == (
        "${{ steps.codex.outputs.sandbox_reaped }}"
    )


def test_sandbox_runtime_pin_is_identical_in_actions_and_hosted_probe() -> None:
    yaml = YAML(typ="safe", pure=True)
    versions = {
        yaml.load((REPO_ROOT / harness / "action.yaml").read_text())["inputs"][
            "sandbox_runtime_version"
        ]["default"]
        for harness in ("claude", "codex")
    }
    workflow = yaml.load((REPO_ROOT / ".github/workflows/ci.yaml").read_text())
    sandbox_steps = workflow["jobs"]["test-sandbox"]["steps"]
    install = next(
        step
        for step in sandbox_steps
        if step.get("name") == "Install pinned Sandbox Runtime capabilities"
    )
    versions.add(install["env"]["SRT_VERSION"])

    assert len(versions) == 1, f"Sandbox Runtime pins diverged: {versions}"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_sandbox_resources_are_removed_immediately_after_agent_reap(
    harness: str,
) -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / harness / "action.yaml").read_text()
    )
    steps = action["runs"]["steps"]
    run_name = "Run Claude" if harness == "claude" else "Run Codex"
    run_at = next(index for index, step in enumerate(steps) if step["name"] == run_name)
    cleanup_at = run_at + (2 if harness == "codex" else 1)
    if harness == "codex":
        stop = steps[run_at + 1]
        assert stop["name"] == "Stop OpenAI Responses proxy"
        assert stop["if"] == "always()"
        assert "/usr/bin/curl" in stop["run"]
    cleanup = steps[cleanup_at]

    assert cleanup["name"] == "Dispose sandbox resources"
    assert cleanup["if"] == "always()"
    assert cleanup["run"].endswith('/dispose_sandbox_resources.py"')
    restore = steps[cleanup_at + 1]
    assert restore["name"] == "Restore Sandbox Runtime host policy"
    assert restore["if"] == "always()"
    assert restore["run"].endswith('/restore-sandbox-runtime-host.sh"')


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_srt_install_receives_trusted_runner_environment(harness: str) -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / harness / "action.yaml").read_text()
    )
    install = next(
        step
        for step in action["runs"]["steps"]
        if step["name"] == "Install Anthropic Sandbox Runtime"
    )

    assert install["env"]["TEND_RUNNER_ENVIRONMENT"] == "${{ runner.environment }}"


def test_srt_host_policy_records_rollback_before_the_host_change() -> None:
    install = (REPO_ROOT / "shared/steps/install-sandbox-runtime.sh").read_text()
    marker = install.index('echo "TEND_RESTORE_APPARMOR_USERNS=true"')
    change = install.index("kernel.apparmor_restrict_unprivileged_userns=0")

    assert marker < change
    assert 'TEND_RUNNER_ENVIRONMENT:-}" = github-hosted' in install
    assert "Leaving self-hosted AppArmor policy unchanged" in install


def test_hosted_srt_probe_launches_only_from_the_action_copy() -> None:
    script = (REPO_ROOT / "proxy" / "test-setup-sandbox.sh").read_text()
    invocation = (
        '"$TEND_TEST_ACTION_PATH/shared/steps/launch_sandbox_runtime.py" || rc=$?'
    )

    assert script.count(invocation) == 2
    assert "-s shared/steps/launch_sandbox_runtime.py" not in script


def test_npm_installs_use_distinct_empty_config_files() -> None:
    install = (
        REPO_ROOT / "shared" / "steps" / "install-sandbox-runtime.sh"
    ).read_text()
    assert 'mktemp "$RUNNER_TEMP/tend-npm-user.XXXXXX"' in install
    assert 'mktemp "$RUNNER_TEMP/tend-npm-global.XXXXXX"' in install
    assert (
        '--userconfig "$npm_userconfig" --globalconfig "$npm_globalconfig"' in install
    )

    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "codex" / "action.yaml").read_text()
    )
    codex_install = next(
        step["run"]
        for step in action["runs"]["steps"]
        if step.get("name") == "Install Codex and Responses proxy"
    )
    assert '--userconfig "$TEND_NPM_USERCONFIG"' in codex_install
    assert '--globalconfig "$TEND_NPM_GLOBALCONFIG"' in codex_install


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_hardened_shells_scrub_bash_env(harness: str) -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / harness / "action.yaml").read_text()
    )

    for step in action["runs"]["steps"]:
        if "--noprofile" in step.get("shell", ""):
            assert step.get("env", {}).get("BASH_ENV") == "", step["name"]


def test_codex_hardened_shells_pin_command_resolution() -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "codex" / "action.yaml").read_text()
    )
    safe_path = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"

    for step in action["runs"]["steps"]:
        if "--noprofile" not in step.get("shell", ""):
            continue

        env = step.get("env", {})
        lines = step["run"].splitlines()
        if len(lines) == 1:
            assert lines[0].startswith(f"{safe_path} /usr/bin/"), step["name"]
            continue

        assert {name: env.get(name) for name in ("BASHOPTS", "SHELLOPTS", "PS4")} == {
            "BASHOPTS": "",
            "SHELLOPTS": "",
            "PS4": "",
        }, step["name"]
        assert lines[0] == "set +x", step["name"]
        assert lines[1] == safe_path or lines[1].startswith("/usr/bin/env -i"), step[
            "name"
        ]


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


def test_generated_workflow_uv_uses_the_action_pin() -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "claude" / "action.yaml").read_text()
    )

    assert UV_VERSION == action["inputs"]["uv_version"]["default"]


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_privileged_sandbox_launch_scrubs_adopter_runtime_configuration(
    harness: str,
) -> None:
    action = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / harness / "action.yaml").read_text()
    )
    step = next(
        step
        for step in action["runs"]["steps"]
        if step.get("name") == "Set up credential-isolation sandbox"
    )
    run = step["run"]

    assert step["env"]["BASH_ENV"] == ""
    assert step["env"]["BASHOPTS"] == ""
    assert step["env"]["SHELLOPTS"] == ""
    assert step["env"]["PS4"] == ""
    assert run.startswith("set +x\n")
    assert "/usr/bin/env -i" in run
    assert "UV_NO_CONFIG=1" in run
    assert "PYTHONNOUSERSITE=1" in run
    assert "--no-python-downloads --python /usr/bin/python3 --script" in run


def test_codex_actions_pin_the_same_cli_version() -> None:
    versions = {
        action: YAML(typ="safe", pure=True).load((REPO_ROOT / action).read_text())[
            "inputs"
        ]["codex_version"]["default"]
        for action in ("codex/action.yaml", "codex/refresh/action.yaml")
    }

    assert len(set(versions.values())) == 1, f"Codex CLI pins differ: {versions}"


# Every `${{ github.action_path }}/…` reference in the composite actions.
# Nothing else reads them: the pre-commit actionlint hook is pinned to
# ^.github/workflows/, so neither action.yaml is linted at all, and no workflow
# here consumes the actions with `uses: ./` — they pin a released ref, so an
# edited body first runs in an adopter's job. A path that resolves nowhere fails
# its step, for every adopter, on the first run after a release.
ACTION_PATH_REF = re.compile(r"\$\{\{\s*github\.action_path\s*\}\}/?([^\s\"')]*)")


COMPOSITE_ACTIONS = (
    "claude/action.yaml",
    "codex/action.yaml",
    "codex/refresh/action.yaml",
)


@pytest.mark.parametrize("action", COMPOSITE_ACTIONS)
def test_action_path_references_resolve(action: str) -> None:
    action_path = REPO_ROOT / action
    body = action_path.read_text()
    matched = ACTION_PATH_REF.findall(body)

    assert matched, f"{action}: no github.action_path references"
    missing = [
        ref
        for ref in sorted(set(matched))
        if not (action_path.parent / ref).resolve().exists()
    ]
    assert not missing, f"{action} references nothing at: {missing}"


# Inline `run:` bodies in the composite actions. Nothing else lints them:
# actionlint only reads workflow files (it parses an action.yaml as a malformed
# workflow — "jobs section is missing"), and the shellcheck hook's `files:`
# regex covers the standalone step scripts, not `claude/` or `codex/`.
# Replace `${{ … }}` with an opaque shell variable before checking composite
# bodies. A literal placeholder would make shellcheck report on the replacement
# instead of the code (`[ -z "literal" ]` → SC2157 "always false").
GHA_EXPR = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


@pytest.mark.parametrize("action", COMPOSITE_ACTIONS)
def test_inline_run_bodies_pass_shellcheck(action: str) -> None:
    """Hold inline step bodies to the same shellcheck the step scripts get.

    Severity matches the shellcheck hook in .pre-commit-config.yaml.
    """
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


# Anchoring on `${{ … }}` would match the bare reference alone, letting
# `${{ inputs.x || '' }}` and `${{ format('{0}', inputs.x) }}` through. Any
# `inputs.<name>` in a `run:` body is necessarily a GHA expression — bash has no
# such syntax — so the unanchored match is both simpler and strictly broader.
# (`inputs['x']` index syntax is missed either way.)
INPUT_REF = re.compile(r"inputs\.([A-Za-z0-9_]+)")


@pytest.mark.parametrize("action", COMPOSITE_ACTIONS)
def test_inputs_reach_run_bodies_through_env(action: str) -> None:
    """An input must not be interpolated into an inline `run:` body.

    GitHub substitutes `${{ … }}` into the script *text* before bash parses it,
    so a value carrying a quote or `$(…)` stops being a string and becomes
    script executing as the runner user — which holds the real PAT and, under
    codex, the model key. Through `env:` the value is passed to the process,
    never to the parser. Nothing else catches this: shellcheck sees the
    placeholder the sibling test substitutes in, and actionlint does not read
    action.yaml at all.

    Every input already arrives this way, so the rule is a flat
    ban rather than a list of which values are secret enough to deserve it.
    """
    doc = YAML(typ="safe", pure=True).load((REPO_ROOT / action).read_text())

    inlined = [
        f"{step.get('name', '<unnamed>')} inlines inputs.{name}"
        for step in doc["runs"]["steps"]
        if "run" in step
        for name in sorted(set(INPUT_REF.findall(step["run"])))
    ]
    assert not inlined, (
        f"{action}: pass these through the step's `env:` instead of `${{{{ }}}}` "
        f"in the body: {inlined}"
    )


def test_codex_action_passes_selected_auth_mode_to_runner() -> None:
    doc = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "codex" / "action.yaml").read_text()
    )
    run = next(step for step in doc["runs"]["steps"] if step.get("name") == "Run Codex")

    assert run["env"]["AUTH_MODE"] == "${{ steps.codex_auth.outputs.mode }}"
    assert run["run"].endswith('/launch_sandbox_runtime.py"')


def test_codex_refresher_keeps_the_secret_writer_pat_out_of_the_model_step() -> None:
    doc = YAML(typ="safe", pure=True).load(
        (REPO_ROOT / "codex" / "refresh" / "action.yaml").read_text()
    )
    steps = {step["name"]: step for step in doc["runs"]["steps"]}

    run = steps["Run Codex refresh"]
    assert run["continue-on-error"] is True
    assert set(run["env"]) == {"CODEX_HOME"}
    publish = steps["Publish refreshed auth"]
    assert publish["if"].startswith("always()")
    assert publish["env"]["GH_TOKEN"] == "${{ inputs.refresh_pat }}"
    assert publish["env"]["CODEX_OUTCOME"] == "${{ steps.codex.outcome }}"
