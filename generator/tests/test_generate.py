"""Smoke tests for workflow generation."""

from __future__ import annotations

import importlib.resources
import re
from pathlib import Path
from textwrap import dedent

import pytest
from tests import ACTION_VERSION
from tests import _yaml as yaml
import click
from click.testing import CliRunner

from tend.cli import main
from tend.config import (
    ANTHROPIC_API_KEY_SECRET,
    BOT_TOKEN_SECRET,
    CLAUDE_TOKEN_SECRET,
    OPENAI_KEY_SECRET,
    Config,
)
from tend.workflows import (
    _deep_merge,
    GENERATORS,
    GeneratedWorkflow,
    generate_all,
    generate_install_test,
    generate_mention,
    generate_review,
)


def _minimal_config(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / ".config" / "tend.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"bot_name: test-bot\n{extra}")
    return cfg


def test_minimal_config_generates_seven_workflows(tmp_path: Path) -> None:
    """ci-fix requires watched_workflows, so minimal config produces seven."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = generate_all(cfg)
    assert len(workflows) == 7
    names = {wf.filename for wf in workflows}
    assert names == {
        "tend-review.yaml",
        "tend-mention.yaml",
        "tend-triage.yaml",
        "tend-nightly.yaml",
        "tend-weekly.yaml",
        "tend-notifications.yaml",
        "tend-review-runs.yaml",
    }


def test_generated_yaml_is_valid(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_config(tmp_path))
    for wf in generate_all(cfg):
        data = yaml.safe_load(wf.content)
        assert isinstance(data, dict), f"{wf.filename} did not parse as dict"
        assert "name" in data, f"{wf.filename} missing name"
        assert "jobs" in data, f"{wf.filename} missing jobs"


def test_disabled_workflow_not_generated(tmp_path: Path) -> None:
    cfg = Config.load(
        _minimal_config(tmp_path, "workflows:\n  weekly:\n    enabled: false\n")
    )
    workflows = generate_all(cfg)
    names = {wf.filename for wf in workflows}
    assert "tend-weekly.yaml" not in names
    assert len(workflows) == 6


def test_setup_steps_rendered(tmp_path: Path) -> None:
    extra = dedent("""\
        setup:
          - uses: ./.github/actions/my-setup
          - run: echo FOO=bar >> $GITHUB_ENV
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert "./.github/actions/my-setup" in wf.content, (
            f"{wf.filename} missing uses step"
        )
        assert "echo FOO=bar >> $GITHUB_ENV" in wf.content, (
            f"{wf.filename} missing run step"
        )


def _eyes_steps(steps: list[dict[str, object]]) -> list[dict[str, object]]:
    """The steps that put the eyes reaction on and take it off, in order."""
    return [s for s in steps if "content=eyes" in str(s.get("run", ""))]


def _restore_step_index(steps: list[dict[str, object]]) -> int | None:
    """Index of the step that puts the loaded tree's local setup actions back."""
    for i, step in enumerate(steps):
        run = str(step.get("run", ""))
        if "git checkout" in run and "$GITHUB_SHA" in run:
            return i
    return None


@pytest.mark.parametrize(
    ("name", "job", "switch", "gate"),
    [
        (
            "review",
            "review",
            lambda s: s.get("uses", "").startswith("actions/checkout")
            and "ref" in s.get("with", {}),
            "steps.gate.outputs.should_run",
        ),
        (
            "mention",
            "handle",
            lambda s: "gh pr checkout" in str(s.get("run", "")),
            None,
        ),
    ],
)
def test_local_setup_action_restored_for_post_cleanup(
    tmp_path: Path,
    name: str,
    job: str,
    switch: object,
    gate: str | None,
) -> None:
    """review and mention land the PR's tree over the workspace a local `setup:`
    composite was loaded from. To dispatch the POST steps of the actions nested
    inside it the runner re-reads that file and matches it against the step list
    it cached at load time, so a PR that resizes or deletes the file fails
    cleanup (actions/runner#2816). Put the loaded version back before the POST
    chain walks.

    `always()` covers a skip as well as a failure, so where the caller gates its
    earlier steps the restore carries that gate too: a gate-skipped run checked
    nothing out, and an unconditional restore would warn about cleaning up a
    composite that never loaded."""
    extra = "setup:\n  - uses: ./.github/actions/tend-setup\n"
    cfg = Config.load(_minimal_config(tmp_path, extra))
    steps = yaml.safe_load(GENERATORS[name](cfg).content)["jobs"][job]["steps"]

    idx = _restore_step_index(steps)
    assert idx is not None, f"{name}: nothing restores the local setup action"
    assert ".github/actions/tend-setup" in str(steps[idx]["run"])
    condition = str(steps[idx].get("if", ""))
    assert condition.startswith("always()"), (
        f"{name}: the restore has to run even when the session fails"
    )
    if gate is None:
        assert condition == "always()", f"{name}: gates at the job level"
    else:
        # Matched by shape, not by substring: a step that disjoined its way
        # past the gate would still contain it.
        assert condition == f"always() && ({gate} == 'true')", (
            f"{name}: the restore has to skip with the steps it restores for"
        )
    switch_idx = next(i for i, s in enumerate(steps) if switch(s))  # type: ignore[operator]
    assert idx > switch_idx, f"{name}: the restore has to follow the tree switch"


def test_restore_step_quotes_the_setup_path(tmp_path: Path) -> None:
    """The path reaches both the `git` operand and the warning text through a
    shell variable, so a `$` can't expand and a `"` can't end the string early
    and fail the one step whose job is to never turn a working run red."""
    extra = "setup:\n  - uses: './weird/$HOME\"; rm -rf /; echo \"'\n"
    cfg = Config.load(_minimal_config(tmp_path, extra))
    steps = yaml.safe_load(generate_mention(cfg).content)["jobs"]["handle"]["steps"]
    run = str(steps[_restore_step_index(steps)]["run"])

    assert """dir='weird/$HOME"; rm -rf /; echo "'""" in run
    assert 'git checkout "$GITHUB_SHA" -- "$dir"' in run
    # Nothing but the single-quoted assignment carries the raw path.
    assert run.count("rm -rf /") == 1


def test_no_restore_step_without_a_local_setup_action(tmp_path: Path) -> None:
    """A remote `uses:` resolves from the action cache, not the workspace, so
    there is nothing to put back."""
    extra = "setup:\n  - uses: astral-sh/setup-uv@v6\n"
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        for job in yaml.safe_load(wf.content)["jobs"].values():
            assert _restore_step_index(job.get("steps", [])) is None, wf.filename


@pytest.mark.parametrize(
    "extra", ["", "setup:\n  - uses: ./.github/actions/tend-setup\n"]
)
def test_generated_workflows_end_with_exactly_one_newline(
    tmp_path: Path, extra: str
) -> None:
    """A trailing blank line is pure churn in the adopter's regen diff, and the
    repo's end-of-file-fixer rejects it in the snapshots."""
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert wf.content.endswith("\n"), f"{wf.filename}: no trailing newline"
        assert not wf.content.endswith("\n\n"), f"{wf.filename}: trailing blank line"


def test_sandbox_levers_rendered_for_claude(tmp_path: Path) -> None:
    """sandbox_path/sandbox_env/sandbox_setup render as action inputs and the
    workflow still parses; the values land under the agent step's `with:`."""
    extra = dedent("""\
        sandbox_path:
          - ~/.cargo/bin
        sandbox_env:
          RUST_BACKTRACE: "1"
        sandbox_setup:
          - rustup component add clippy
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    wf = generate_mention(cfg)
    data = yaml.safe_load(wf.content)
    with_blocks = [
        s["with"]
        for job in data["jobs"].values()
        for s in job.get("steps", [])
        if "sandbox_path" in s.get("with", {})
    ]
    assert len(with_blocks) == 1
    with_block = with_blocks[0]
    assert with_block["sandbox_path"].strip() == "~/.cargo/bin"
    assert with_block["sandbox_env"].strip() == "RUST_BACKTRACE=1"
    assert with_block["sandbox_setup"].strip() == "rustup component add clippy"


def _agent_step_inputs(content: str) -> list[set[str]]:
    """The `with:` keys of each composite-action step in a generated workflow.

    Structural rather than a substring search over the file, so workflow
    comments naming a config key don't read as the input being threaded.
    """
    data = yaml.safe_load(content)
    return [
        set(step.get("with", {}))
        for job in data["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("max-sixty/tend/")
    ]


def test_sandbox_levers_absent_by_default(tmp_path: Path) -> None:
    levers = {"sandbox_path", "sandbox_env", "sandbox_setup"}
    cfg = Config.load(_minimal_config(tmp_path))
    for wf in generate_all(cfg):
        for inputs in _agent_step_inputs(wf.content):
            assert not levers & inputs


def test_sandbox_levers_not_rendered_for_codex(tmp_path: Path) -> None:
    """Codex runs on the runner (no proxy sandbox), so the sandbox_* inputs are
    not threaded — a codex adopter uses `setup:` instead."""
    extra = dedent("""\
        harness: codex
        model: gpt-5.5
        sandbox_path:
          - ~/.cargo/bin
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        for inputs in _agent_step_inputs(wf.content):
            assert "sandbox_path" not in inputs


def test_setup_uses_with_parameters_gets_if_guard(tmp_path: Path) -> None:
    """A `uses` setup step with `with:` parameters must still receive the
    `if:` guard in the notifications workflow.

    Without `with` support on `uses`, steps like `actions/setup-node@v4` that
    require parameters are forced into `raw`, which cannot receive the guard —
    so they run even when the pre-check has skipped checkout, failing with
    "The specified node version file does not exist" (issue #281).
    """
    extra = dedent("""\
        setup:
          - uses: actions/setup-node@v4
            with:
              node-version-file: .node-version
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    notifications = workflows["tend-notifications.yaml"]
    data = yaml.safe_load(notifications.content)

    steps = data["jobs"]["notifications"]["steps"]
    setup_node = next(
        (s for s in steps if s.get("uses") == "actions/setup-node@v4"), None
    )
    assert setup_node is not None, "setup-node step missing from notifications workflow"
    assert setup_node.get("with") == {"node-version-file": ".node-version"}, (
        "uses step must render `with:` parameters"
    )
    assert "if" in setup_node, (
        "setup-node step must receive the `if:` guard so it is skipped when "
        "checkout was skipped (otherwise .node-version is missing and the "
        "step fails)"
    )


def test_setup_step_passthrough_fields(tmp_path: Path) -> None:
    """Any GitHub step field (env, name, shell, working-directory, etc.) flows
    through on a structured step, so users don't need `raw` just to pass them.
    """
    extra = dedent("""\
        setup:
          - uses: actions/setup-node@v4
            name: Setup Node
            with:
              node-version-file: .node-version
            env:
              FORCE_COLOR: "1"
          - run: cargo build --release
            shell: bash
            working-directory: ./crates/core
            env:
              RUSTFLAGS: -D warnings
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    review = workflows["tend-review.yaml"]
    data = yaml.safe_load(review.content)

    steps = data["jobs"]["review"]["steps"]
    node = next(s for s in steps if s.get("uses") == "actions/setup-node@v4")
    assert node["name"] == "Setup Node"
    assert node["with"] == {"node-version-file": ".node-version"}
    assert node["env"] == {"FORCE_COLOR": "1"}

    build = next(s for s in steps if s.get("run") == "cargo build --release")
    assert build["shell"] == "bash"
    assert build["working-directory"] == "./crates/core"
    assert build["env"] == {"RUSTFLAGS": "-D warnings"}


@pytest.mark.parametrize(
    ("filename", "job", "guard"),
    [
        (
            "tend-notifications.yaml",
            "notifications",
            "steps.check.outputs.count != '0' || github.event_name == 'workflow_dispatch'",
        ),
        ("tend-review.yaml", "review", "steps.gate.outputs.should_run == 'true'"),
    ],
)
def test_setup_step_user_if_narrows_the_pre_check_guard(
    tmp_path: Path,
    filename: str,
    job: str,
    guard: str,
) -> None:
    """A user-supplied `if:` narrows the workflow's pre-check guard; it does not
    replace it. Replacing it is unsafe in a way the adopter can't see: on a run
    the pre-check declined nothing is checked out, so a surviving
    `uses: ./…` step resolves against an empty workspace and fails the job."""
    extra = dedent("""\
        setup:
          - run: ./flaky.sh
            if: "runner.os == 'Linux'"
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}

    data = yaml.safe_load(workflows[filename].content)
    step = next(s for s in data["jobs"][job]["steps"] if s.get("run") == "./flaky.sh")
    # Both sides bracketed: `&&` binds tighter than `||`, and notifications'
    # guard is a disjunction — `A || B && (own)` would apply the adopter's
    # condition to B alone.
    assert step["if"] == f"({guard}) && (runner.os == 'Linux')"


def test_setup_step_rejects_unknown_field(tmp_path: Path) -> None:
    """Typos in step field names fail at config load, not at workflow parse."""
    extra = dedent("""\
        setup:
          - uses: actions/checkout@v4
            continue-on-errors: true
    """)
    with pytest.raises(click.ClickException, match="unknown field.*continue-on-errors"):
        Config.load(_minimal_config(tmp_path, extra))


def test_setup_step_env_must_be_table(tmp_path: Path) -> None:
    extra = dedent("""\
        setup:
          - run: echo hi
            env: "not a mapping"
    """)
    with pytest.raises(click.ClickException, match="`env` must be a mapping"):
        Config.load(_minimal_config(tmp_path, extra))


def test_empty_setup_no_blank_lines(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_config(tmp_path))
    for wf in generate_all(cfg):
        assert "\n\n\n" not in wf.content, f"{wf.filename} has triple blank lines"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_workflows_read_only_the_operational_secrets(
    tmp_path: Path, harness: str
) -> None:
    """Every stored secret a workflow reads is one `tend check` verifies.

    The names are fixed constants shared by the templates and the checks, so
    the failure this guards is a template naming a secret that nothing
    provisions — which surfaces only as an empty token at run time. The set
    is per harness because the checks are: `check_claude_auth` verifies the
    Claude pair and `check_codex_auth` the OpenAI key, so a claude workflow
    reading `secrets.OPENAI_API_KEY` is unprovisioned as surely as one
    reading a name nothing defines. `secrets.GITHUB_TOKEN` is
    workflow-scoped rather than stored, so it is outside the set."""
    verified = {BOT_TOKEN_SECRET} | (
        {CLAUDE_TOKEN_SECRET, ANTHROPIC_API_KEY_SECRET}
        if harness == "claude"
        else {OPENAI_KEY_SECRET}
    )
    cfg = Config.load(_minimal_config(tmp_path, f"harness: {harness}\n"))
    for wf in generate_all(cfg):
        read = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", wf.content))
        assert read - {"GITHUB_TOKEN"} <= verified, (
            f"{wf.filename} reads a secret the {harness} harness does not "
            f"provision: {sorted(read - {'GITHUB_TOKEN'} - verified)}"
        )
        assert BOT_TOKEN_SECRET in read, f"{wf.filename} missing the bot token"


def test_claude_workflows_emit_both_auth_inputs(tmp_path: Path) -> None:
    """Claude agent step references both OAuth token and API key secrets."""
    cfg = Config.load(_minimal_config(tmp_path))
    for wf in generate_all(cfg):
        assert (
            "claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
            in wf.content
        ), f"{wf.filename} missing claude_code_oauth_token input"
        assert "anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}" in wf.content, (
            f"{wf.filename} missing anthropic_api_key input"
        )
        assert "openai_api_key" not in wf.content, (
            f"{wf.filename} should not reference openai_api_key under claude"
        )


def test_operational_secrets_imply_environment(tmp_path: Path) -> None:
    """A job names the environment exactly when it reads an operational secret.

    The environment's deployment branch policy is what keeps a pushed workflow
    from reading the secrets out of its own run, so a secret-bearing job that
    forgets to name it reopens that hole. The converse matters too: a job
    holding no secret (mention's relay) must not name it, or it loses the refs
    the policy excludes for nothing — and for the relay those refs are the
    whole point. `secrets.GITHUB_TOKEN` is workflow-scoped, not stored, so it
    doesn't count. install-test is included: it runs on `pull_request`, whose
    merge ref the policy refuses, so it may neither read a secret nor name
    the environment. ci-fix is enabled here so the corpus is every workflow:
    without `watched_workflows` the generator skips it, and a skipped
    workflow is one this invariant silently stops covering."""

    def _strings(x: object) -> list[str]:
        if isinstance(x, str):
            return [x]
        if isinstance(x, dict):
            return [s for v in x.values() for s in _strings(v)]
        if isinstance(x, list):
            return [s for v in x for s in _strings(v)]
        return []

    cfg = Config.load(
        _minimal_config(
            tmp_path, 'workflows:\n  ci-fix:\n    watched_workflows: ["ci"]\n'
        )
    )
    generated = generate_all(cfg, with_install_test=True)
    # Assert the corpus is *every* workflow, not that one name is present:
    # the next generator with a config precondition would otherwise drop out
    # of this invariant as silently as ci-fix did.
    assert {wf.filename for wf in generated} == {
        f"tend-{name}.yaml" for name in GENERATORS
    } | {"tend-install-test.yaml"}
    for wf in generated:
        data = yaml.safe_load(wf.content)
        for job_name, job in data["jobs"].items():
            reads_secret = any(
                ref != "secrets.GITHUB_TOKEN"
                for s in _strings(job)
                for ref in re.findall(r"secrets\.[A-Za-z0-9_]+", s)
            )
            # `deployment: false` is part of the asserted shape, not just the
            # name: dropping it leaves the gate working and costs only a
            # deployment record per run, which GitHub posts as a "<bot>
            # deployed to tend" line on every PR the run belongs to. Nothing
            # else would fail, so this is where it gets caught.
            names_environment = job.get("environment") == {
                "name": "tend",
                "deployment": False,
            }
            assert names_environment == reads_secret, f"{wf.filename}:{job_name} " + (
                "reads an operational secret without naming the environment "
                "as `{name: tend, deployment: false}`"
                if reads_secret
                else "names the environment but holds no secret"
            )


def test_custom_prompt(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          triage:
            prompt: "Custom triage: {issue_number}"
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    triage = workflows["tend-triage.yaml"]
    assert "Custom triage:" in triage.content


def _review_prompt(review: GeneratedWorkflow) -> str:
    """The `prompt:` input the review job hands the harness action."""
    steps = yaml.safe_load(review.content)["jobs"]["review"]["steps"]
    step = next(
        s for s in steps if s.get("uses", "").startswith("max-sixty/tend/claude@")
    )
    return step["with"]["prompt"]


def test_review_prompt_without_placeholder_keeps_literal_braces(
    tmp_path: Path,
) -> None:
    """A review prompt with braces but no `{pr_number}` reaches the agent verbatim.

    The review prompt is the only one emitted inside a GHA expression. With the
    placeholder it goes through `format()`, which needs every other brace
    doubled; without it, it is a bare string literal that GHA never collapses,
    so doubling there would ship `{{...}}` to the agent.
    """
    extra = dedent("""\
        workflows:
          review:
            prompt: "Review this PR. Skip files matching {generated}."
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    prompt = _review_prompt(workflows["tend-review.yaml"])
    assert "{generated}" in prompt
    assert "{{generated}}" not in prompt
    assert "format(" not in prompt


def test_review_prompt_with_placeholder_escapes_other_braces(tmp_path: Path) -> None:
    """With `{pr_number}` present the prompt goes through `format()`, so the
    placeholder becomes `{0}` and every other brace is doubled for it."""
    extra = dedent("""\
        workflows:
          review:
            prompt: "Review PR {pr_number}. Skip files matching {generated}."
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    prompt = _review_prompt(workflows["tend-review.yaml"])
    assert "format(" in prompt
    assert "{0}" in prompt
    assert "{{generated}}" in prompt


def test_watched_workflows(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          ci-fix:
            watched_workflows: ["build", "test", "lint"]
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    ci_fix = workflows["tend-ci-fix.yaml"]
    assert '"build"' in ci_fix.content
    assert '"test"' in ci_fix.content
    assert '"lint"' in ci_fix.content
    assert 'branches: ["main"]' in ci_fix.content


def test_ci_fix_custom_branches(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          ci-fix:
            watched_workflows: ["ci"]
            branches: ["main", "release"]
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    ci_fix = workflows["tend-ci-fix.yaml"]
    assert 'branches: ["main", "release"]' in ci_fix.content


def test_cli_init_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dry-run"])
    assert result.exit_code == 0
    assert "tend-review.yaml" in result.output
    # Dry run should not create files
    assert not (tmp_path / ".github" / "workflows").exists()


def test_cli_init_writes_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0
    assert "Generated 7 workflow files" in result.output
    wf_dir = tmp_path / ".github" / "workflows"
    assert wf_dir.exists()
    assert len(list(wf_dir.glob("tend-*.yaml"))) == 7


def test_review_probes_merge_ref_and_falls_back_to_head(tmp_path: Path) -> None:
    """tend-review must probe refs/pull/N/merge and fall back to /head on 404.

    GitHub only materializes the merge ref for mergeable PRs, so without a
    fallback the checkout 404s on every conflicting PR and the whole review
    job cascades as skipped. The probe step wires its output into checkout's
    `ref:` so review always runs.
    """
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    steps = data["jobs"]["review"]["steps"]
    probe_idx = next(i for i, s in enumerate(steps) if s.get("id") == "pr_ref")
    checkout_idx = _pr_tree_checkout_idx(steps)
    assert probe_idx < checkout_idx
    probe = steps[probe_idx]
    assert "gh api" in probe["run"]
    assert "refs/pull/$PR/merge" in probe["run"]
    assert "refs/pull/$PR/head" in probe["run"]
    assert steps[checkout_idx]["with"]["ref"] == "${{ steps.pr_ref.outputs.ref }}"


def _pr_tree_checkout_idx(steps: list[dict]) -> int:
    """Index of review's second checkout — the one carrying the fork PR ref."""
    return next(
        i
        for i, s in enumerate(steps)
        if s.get("uses") == "actions/checkout@v7" and "ref" in s.get("with", {})
    )


def test_setup_runs_on_base_tree_in_review(tmp_path: Path) -> None:
    """Review checks out the base tree, runs `setup:`, then lands the PR tree.

    `setup:` runs as the runner user, outside the sandbox the harness builds
    and before it strips the checkout PAT from `.git/config`. Against the PR
    tree it would execute a contributor's build backend, dependencies, and
    local `uses: ./` actions with that access, which is the boundary the
    sandbox exists to draw. The PR checkout keeps `clean: false` so it does
    not delete what setup wrote into the workspace.
    """
    extra = "setup:\n  - uses: ./.github/actions/my-setup\n"
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    steps = data["jobs"]["review"]["steps"]

    base_idx = next(
        i for i, s in enumerate(steps) if s.get("uses") == "actions/checkout@v7"
    )
    setup_idx = next(
        i for i, s in enumerate(steps) if s.get("uses") == "./.github/actions/my-setup"
    )
    pr_idx = _pr_tree_checkout_idx(steps)

    assert base_idx < setup_idx < pr_idx
    assert "ref" not in steps[base_idx].get("with", {}), (
        "the pre-setup checkout must take the event's base ref, not a fork ref"
    )
    assert steps[pr_idx]["with"]["clean"] is False


def test_review_without_setup_checks_out_once(tmp_path: Path) -> None:
    """The base checkout is setup's, so it renders only alongside setup.

    With nothing to run against the base tree, a second full-history clone is
    pure overhead. `clean` goes with it — the action consults it only when it
    finds an existing repo, which a lone checkout never does.
    """
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    steps = data["jobs"]["review"]["steps"]

    checkouts = [s for s in steps if s.get("uses") == "actions/checkout@v7"]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["ref"] == "${{ steps.pr_ref.outputs.ref }}"
    assert "clean" not in checkouts[0]["with"]


def test_review_queues_pushes_behind_a_gate(tmp_path: Path) -> None:
    """A push mid-review queues a replacement run; the gate step decides
    whether it boots an agent.

    The running session folds the push in and posts its review at the live head,
    so the queued run's work is usually already done. That only holds if the
    gate is the first step and everything after it — checkouts, setup, the agent
    — is conditioned on its verdict; an ungated step would run (and bill) on
    every replaced event. Every event still waits for the current session:
    `queue: max` is what keeps a push from replacing a pending
    ready-for-review.

    The second setup step carries its own `if:`, the one shape that could slip
    past the guard: an ungated `uses: ./…` below a gated checkout would resolve
    against an empty workspace on every skipped run.
    """
    extra = (
        "setup:\n"
        "  - run: npm ci\n"
        "  - run: ./optional.sh\n"
        "    if: \"runner.os == 'Linux'\"\n"
    )
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    content = workflows["tend-review.yaml"].content
    data = yaml.safe_load(content)
    job = data["jobs"]["review"]

    assert job["concurrency"]["cancel-in-progress"] is False
    assert job["concurrency"]["queue"] == "max"
    assert "must set\n      # `queue: null`" in content
    steps = job["steps"]
    assert steps[0].get("id") == "gate"
    # Unconditional: the step that decides cannot itself be skipped, and a
    # skipped step's outputs are empty, which reads as "don't run".
    assert "if" not in steps[0]
    # The gate reads published review state. It must not go back to a commit
    # status — writing one put a visible check row on every reviewed commit,
    # and reading one is how the removed gate consumed it.
    assert "/reviews?per_page=100" in steps[0]["run"]
    assert "tend-review/" not in content
    assert "/statuses/" not in content
    assert "/status?" not in content
    # A crashing gate must paint the job red, not leave every later step
    # skipped on an empty output — a review that silently never happened.
    assert "continue-on-error" not in steps[0]

    gate = "steps.gate.outputs.should_run == 'true'"
    # The two steps that undo what the run did: they have to survive a failed
    # session, so `always()` is right there and wrong everywhere else.
    cleanup = {
        "Restore local setup actions for POST cleanup",
        "Remove the eyes reaction",
    }
    for step in steps[1:]:
        condition = str(step.get("if", ""))
        if step.get("name") in cleanup:
            assert condition == f"always() && ({gate})", f"cleanup step: {step}"
            continue
        # Any status function in an `if` drops GHA's implicit `success() &&`,
        # so a working step carrying one would run on the workspace a failed
        # checkout left behind. Matched by shape, not by substring: a step
        # disjoining its way past the gate would still contain it.
        assert not any(
            fn in condition for fn in ("always()", "failure()", "cancelled()")
        ), f"working step opts out of success(): {step}"
        assert condition == gate or condition.startswith(f"({gate}) && "), (
            f"ungated step after the gate: {step}"
        )


def test_issue_and_pr_acknowledged_with_eyes(tmp_path: Path) -> None:
    """An issue or PR the bot takes up carries an eyes reaction before the agent
    boots: the session takes minutes to reach its first comment, and until then
    the author has nothing telling them the bot picked the event up.

    So the reaction goes on wherever a session starts, not only on the first
    one — a push mid-review that boots its own session earns its own eyes.
    """
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}

    triage = yaml.safe_load(workflows["tend-triage.yaml"].content)
    first = triage["jobs"]["triage"]["steps"][0]
    assert "content=eyes" in first["run"]
    assert first["env"]["TARGET"] == "issues/${{ github.event.issue.number }}"
    # Every issues:opened event the job-level `if` admits is the bot's to take.
    assert "if" not in first

    review = yaml.safe_load(workflows["tend-review.yaml"].content)
    react = _eyes_steps(review["jobs"]["review"]["steps"])[0]
    assert react["env"]["TARGET"] == "issues/${{ github.event.pull_request.number }}"
    # Nothing beyond the gate: a run that boots an agent says so.
    assert react["if"] == "steps.gate.outputs.should_run == 'true'"


@pytest.mark.parametrize(
    ("name", "job"),
    [("triage", "triage"), ("review", "review"), ("mention", "handle")],
)
def test_eyes_come_off_when_the_session_ends(
    tmp_path: Path, name: str, job: str
) -> None:
    """👀 means a session is working on this right now, so every workflow that
    puts it on takes it off again — under `always()`, so a failed or cancelled
    run doesn't strand it.

    Both halves live in the *same* job, which is what makes `always()` mean
    anything: a job cancelled while still queued never allocates a runner, so
    none of its steps execute — `always()` governs execution within a job that
    started. React in one job and unreact in another and every route where the
    second job never starts leaves the eyes on with no session behind them.
    For jobs with the default one-pending-run queue, `cancel-in-progress: false`
    doesn't close that: it holds a *running* job while GitHub can still evict a
    *pending* one. Review's `queue: max` prevents ordinary eviction, but keeping
    both halves in one job preserves the same lifecycle invariant.

    Both halves also have to name the same reaction target; one that drifted
    would leave the eyes on every comment the bot ever answered."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    jobs = yaml.safe_load(workflows[f"tend-{name}.yaml"].content)["jobs"]

    reacting = {j for j, spec in jobs.items() if _eyes_steps(spec["steps"])}
    assert reacting == {job}, f"{name}: the eyes are split across {reacting}"

    eyes = _eyes_steps(jobs[job]["steps"])
    react, unreact = eyes[0], eyes[-1]
    assert "-X DELETE" in unreact["run"], f"{name}: nothing removes the reaction"
    assert unreact["if"].startswith("always()"), (
        f"{name}: a failed session has to release the reaction too"
    )
    assert unreact["env"]["TARGET"] == react["env"]["TARGET"]
    # The bot's own reaction only — a human's 👀 on the same issue stays put.
    assert 'select(.user.login == \\"$BOT_NAME\\")' in unreact["run"]
    # The bot's 👀 is one of many on a busy thread, and page 1 is 30 of them:
    # unpaginated, the lookup misses its own reaction and silently keeps it.
    assert "--paginate" in unreact["run"], (
        f"{name}: the reaction lookup only reads the first page"
    )


def test_setup_raw_rejected_with_migration_hint(tmp_path: Path) -> None:
    """`raw` was removed in favor of structured steps — the error message
    must point users at the two supported paths so they can migrate."""
    extra = dedent("""\
        setup:
          - raw: |
              - uses: Swatinem/rust-cache@v2
                with:
                  save-if: false
    """)
    with pytest.raises(click.ClickException, match="composite action"):
        Config.load(_minimal_config(tmp_path, extra))


def test_mention_handles_pull_request_review(tmp_path: Path) -> None:
    """A submitted review must still reach the bot on an engaged PR — via the
    secretless relay job, which re-enters the event as a repository_dispatch so
    the run holding the secrets carries a ref their environment admits."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)

    # The review events' own runs carry refs/pull/N/merge, which the secrets'
    # environment does not admit, so they may reach only the relay job.
    assert data["on"]["pull_request_review"] == {"types": ["submitted"]}
    assert data["on"]["repository_dispatch"] == {"types": ["tend-mention-review"]}
    relay = data["jobs"]["relay"]
    assert "pull_request_review" in relay["if"]
    # The relay holds no secrets, so it is where the fork filter now lives.
    assert "pull_request.head.repo.full_name" in relay["if"]
    assert "environment" not in relay, (
        "the relay must stay outside the secrets' environment — naming it "
        "would block the very refs it exists to accept"
    )
    # `contents: write` is what POST /dispatches requires — probed: an
    # identical same-repo run with `contents: read` is refused 403, which
    # would leave every review mention unanswered. Pinned as the whole set,
    # so a scope added here has to be argued for.
    assert relay["permissions"] == {"contents": "write"}
    # Identifiers only: verify re-reads the review from the API, so nothing
    # judged downstream comes from the forgeable dispatch payload.
    relay_run = relay["steps"][-1]["run"]
    assert "client_payload[kind]" in relay_run
    assert "client_payload[pr]" in relay_run
    assert "client_payload[id]" in relay_run
    assert "client_payload[url]" not in relay_run

    # The secret-bearing jobs must not run on the review events themselves.
    verify_if = data["jobs"]["verify"]["if"]
    assert "repository_dispatch" in verify_if
    assert "pull_request_review" not in verify_if

    # Handle job checks out PR branch for this event
    handle_steps = data["jobs"]["handle"]["steps"]
    checkout_step = next(
        s for s in handle_steps if s.get("name") == "Check out PR branch"
    )
    assert "repository_dispatch" in checkout_step["if"]

    # Prompt keeps the review-kind and mention/participation branches apart,
    # keyed on the payload's kind and verify's judged reason — never on a
    # reason the payload itself claims.
    tend_step = next(
        s
        for s in handle_steps
        if s.get("uses", "").startswith("max-sixty/tend/claude@")
    )
    prompt = tend_step["with"]["prompt"]
    assert "needs.verify.outputs.reason == 'mention'" in prompt
    assert "needs.verify.outputs.url" in prompt
    assert "client_payload.kind == 'pull_request_review'" in prompt
    assert "client_payload.reason" not in prompt


def test_mention_review_comment_listens_only_for_edits(tmp_path: Path) -> None:
    """pull_request_review_comment must subscribe to `edited` only, not `created`.

    Modern GitHub fires *both* pull_request_review and pull_request_review_comment
    for every newly-created inline comment (verified across the standalone
    POST /pulls/{n}/comments endpoint, the /replies endpoint, the "Add single
    comment" UI button, and reviews submitted with inline comments). If we
    subscribed to `created` here, the duplicate run would collide on the
    tend-mention-handle-<PR#> concurrency group, the loser would be cancelled,
    and the cancelled check_run on the PR head SHA would render the PR's
    statusCheckRollup as FAILURE — even though the bot did its job from the
    sibling run.

    Edits have no sibling event (review submissions don't fire on edits), so
    we still need to listen for `edited` to catch edit-to-summon ("@bot" added
    to an existing comment after the fact)."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)
    assert data["on"]["pull_request_review_comment"] == {"types": ["edited"]}, (
        "pull_request_review_comment must subscribe to ['edited'] only — see "
        "the trigger comment in the mention template for the dedup rationale"
    )


def test_review_gate_wires_every_variable_it_reads(tmp_path: Path) -> None:
    """The gate's decisions are exercised by running it (test_shared_steps); what
    generation owns is the wiring. The script is inlined verbatim, so a name the
    workflow forgets is simply empty at runtime — a missing BOT_NAME matches no
    review author, and every push then boots an agent on a green job."""
    cfg = Config.load(_minimal_config(tmp_path))
    data = yaml.safe_load(generate_review(cfg).content)
    gate_step = next(
        s for s in data["jobs"]["review"]["steps"] if s.get("id") == "gate"
    )

    assert gate_step["env"] == {
        "GITHUB_TOKEN": f"${{{{ secrets.{BOT_TOKEN_SECRET} }}}}",
        "PR": "${{ github.event.pull_request.number }}",
        # `synchronize` and `reopened` are the actions the gate can skip; the
        # rest ask for a pass whatever the head already carries.
        "EVENT_ACTION": "${{ github.event.action }}",
        # Whose reviews count as an anchor. The configured identity, so the
        # gate needs no API call to learn who it is.
        "BOT_NAME": "test-bot",
    }

    source = (
        importlib.resources.files("tend") / "templates" / "review-gate.sh"
    ).read_text()
    read = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", source))
    assigned = set(re.findall(r"\b([A-Z][A-Z0-9_]*)=", source))
    supplied = set(gate_step["env"]) | {"GITHUB_OUTPUT", "GITHUB_REPOSITORY"}
    assert read - assigned <= supplied, (
        f"the gate reads {sorted(read - assigned - supplied)}, which nothing sets"
    )


def test_mention_verify_wires_every_variable_the_gate_reads(tmp_path: Path) -> None:
    """The gate's own decisions are exercised by running it (test_mention_verify);
    what generation owns is the wiring, and an unwired variable is invisible
    there. The script is inlined verbatim, so a name the workflow forgets is
    simply empty at runtime: the gate then answers on a blank — a missing
    COMMENT_AUTHOR reads as "not the bot", a missing PR_URL as "an issue, not a
    PR" — silently, on a green job."""
    cfg = Config.load(_minimal_config(tmp_path))
    data = yaml.safe_load(generate_mention(cfg).content)
    check_step = next(
        s for s in data["jobs"]["verify"]["steps"] if s.get("id") == "check"
    )

    assert check_step["env"] == {
        "GITHUB_TOKEN": f"${{{{ secrets.{BOT_TOKEN_SECRET} }}}}",
        "BOT_NAME": "test-bot",
        "EVENT_NAME": "${{ github.event_name }}",
        "COMMENT_BODY": "${{ github.event.comment.body }}",
        "COMMENT_AUTHOR": "${{ github.event.comment.user.login }}",
        # The public-API discriminator between a Bot account and a User.
        "COMMENT_AUTHOR_TYPE": "${{ github.event.comment.user.type }}",
        "ISSUE_BODY": "${{ github.event.issue.body }}",
        "ISSUE_OR_PR_NUMBER": "${{ github.event.issue.number }}",
        "ISSUE_AUTHOR": "${{ github.event.issue.user.login }}",
        # Present only on comments that live on a PR, which is how the gate
        # tells an issue thread from a PR one.
        "PR_URL": "${{ github.event.issue.pull_request.url }}",
        # A relayed review arrives as identifiers only; the gate resolves them
        # against the API before judging anything.
        "PAYLOAD_KIND": "${{ github.event.client_payload.kind }}",
        "PAYLOAD_PR": "${{ github.event.client_payload.pr }}",
        "PAYLOAD_ID": "${{ github.event.client_payload.id }}",
    }

    # And the mapping is complete: every name the script reads without first
    # assigning it is either wired above or supplied by the runner.
    source = (
        importlib.resources.files("tend") / "templates" / "mention-verify.sh"
    ).read_text()
    read = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", source))
    assigned = set(re.findall(r"\b([A-Z][A-Z0-9_]*)=", source))
    supplied = set(check_step["env"]) | {"GITHUB_OUTPUT", "GITHUB_REPOSITORY"}
    assert read - assigned <= supplied, (
        f"the gate reads {sorted(read - assigned - supplied)}, which nothing sets"
    )


def test_mention_verify_no_concurrency(tmp_path: Path) -> None:
    """verify job must not have concurrency — a non-mention comment can cancel
    an explicit @bot mention if both arrive on the same PR within seconds (#93)."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)
    verify = data["jobs"]["verify"]
    assert "concurrency" not in verify, (
        "verify job must not have concurrency — rapid comments on the same PR "
        "can cancel an explicit @bot mention (#93)"
    )


def test_mention_handle_job_queues_not_cancels(tmp_path: Path) -> None:
    """The handle job must queue (not cancel) to avoid dropping mentions (#93)."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)
    handle = data["jobs"]["handle"]
    assert "concurrency" in handle, (
        "handle job must have concurrency to prevent duplicate runs"
    )
    assert handle["concurrency"]["cancel-in-progress"] is False, (
        "handle must queue (cancel-in-progress: false) so mentions aren't dropped"
    )


def test_setup_before_pr_checkout_in_mention(tmp_path: Path) -> None:
    """Setup runs against the default branch, before switching to the PR branch.

    A PR opened before a referenced local composite action existed (and never
    rebased) carries a tree without that action; running setup after
    `gh pr checkout` would 404 with `Can't find 'action.yml'` and drop the
    maintainer's mention silently.
    """
    extra = "setup:\n  - uses: ./.github/actions/my-setup\n"
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    initial_checkout_idx = mention.content.index("actions/checkout@v7")
    setup_idx = mention.content.index("./.github/actions/my-setup")
    pr_checkout_idx = mention.content.index("Check out PR branch")
    assert initial_checkout_idx < setup_idx < pr_checkout_idx, (
        "Setup must run after the initial checkout and before PR-branch switch"
    )


def test_mention_handle_has_queue_delay(tmp_path: Path) -> None:
    """Handle job computes queue delay so the prompt can detect stale triggers."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)
    handle_steps = data["jobs"]["handle"]["steps"]
    delay_steps = [s for s in handle_steps if s.get("id") == "delay"]
    assert len(delay_steps) == 1, "handle job must have a queue delay step"
    assert "steps.delay.outputs.seconds" in mention.content, (
        "prompt must reference queue delay"
    )
    # Delay step must come before the tend action (output must be available)
    delay_idx = mention.content.index("Compute queue delay")
    tend_idx = mention.content.index(f"max-sixty/tend/claude@{ACTION_VERSION}")
    assert delay_idx < tend_idx, "delay step must precede tend action"


def test_mention_queue_delay_guards_empty_event_ts(tmp_path: Path) -> None:
    """date -d "" silently returns now on GNU; guard against empty EVENT_TS."""
    cfg = Config.load(_minimal_config(tmp_path))
    wf = generate_mention(cfg)
    data = yaml.safe_load(wf.content)
    delay_step = next(
        s for s in data["jobs"]["handle"]["steps"] if s.get("id") == "delay"
    )
    script = delay_step["run"]
    # Must bail before date -d when EVENT_TS is empty
    assert 'if [ -z "$EVENT_TS" ]' in script
    # date -d must only run after the guard
    guard_pos = script.index('-z "$EVENT_TS"')
    date_pos = script.index("date -d")
    assert guard_pos < date_pos, "empty guard must precede date -d call"


def test_mention_prompt_omits_delay_when_empty(tmp_path: Path) -> None:
    """Prompt preamble must not hardcode delay text — it should be conditional
    so an empty seconds output doesn't produce broken prose like 's after'."""
    cfg = Config.load(_minimal_config(tmp_path))
    wf = generate_mention(cfg)
    data = yaml.safe_load(wf.content)
    tend_step = next(
        s
        for s in data["jobs"]["handle"]["steps"]
        if s.get("uses", "").startswith("max-sixty/tend/claude@")
    )
    prompt = tend_step["with"]["prompt"]
    # The delay text must be inside a format() conditional, not hardcoded
    assert "format(" in prompt, "delay preamble must use conditional format()"
    # "Before acting" must always appear (it's the unconditional part)
    assert "Before acting" in prompt


# ---------------------------------------------------------------------------
# Fork guard
# ---------------------------------------------------------------------------


# Filenames whose only triggers are `schedule`, `workflow_dispatch`,
# `workflow_run`, or `issues` — events that can fire from a fork's own Actions
# once Actions is enabled there. Without the guard, the `tend` action step fails
# noisily because the bot/Claude secrets are empty in the fork's secret store.
_GUARDED_WORKFLOWS = [
    "tend-ci-fix.yaml",
    "tend-nightly.yaml",
    "tend-weekly.yaml",
    "tend-review-runs.yaml",
    "tend-notifications.yaml",
    "tend-triage.yaml",
]
# tend-review uses pull_request_target (base repo only); tend-mention's
# review-event paths already filter forks, and `issues`/`issue_comment` events
# are unguarded by design (forks rarely enable Issues, and gating here would
# silently drop legitimate same-repo activity if the owner is misconfigured).
_UNGUARDED_WORKFLOWS = ["tend-review.yaml", "tend-mention.yaml"]


@pytest.mark.parametrize("filename", _GUARDED_WORKFLOWS)
def test_fork_guard_present_when_repo_owner_set(tmp_path: Path, filename: str) -> None:
    """Each fork-exposed workflow must skip on owner mismatch.

    `cli.init` injects `repo_owner` from the local git remote; here we set it
    on the loaded Config to mirror that injection in a unit-test context.
    """
    name = filename.removeprefix("tend-").removesuffix(".yaml")
    cfg = Config.load(_minimal_config(tmp_path, _extra_for(name)))
    cfg.repo_owner = "test-owner"
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows[filename].content)
    job_ifs = [j.get("if", "") for j in data["jobs"].values()]
    assert any("github.repository_owner == 'test-owner'" in cond for cond in job_ifs), (
        f"{filename} job must include the fork guard (job ifs: {job_ifs})"
    )


@pytest.mark.parametrize("filename", _UNGUARDED_WORKFLOWS)
def test_fork_guard_absent_for_unguarded(tmp_path: Path, filename: str) -> None:
    """tend-review (pull_request_target) and tend-mention (own filtering) must
    not get a job-level repo_owner guard — adding one would drop legitimate
    activity on those workflows if owner is misconfigured."""
    cfg = Config.load(_minimal_config(tmp_path))
    cfg.repo_owner = "test-owner"
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows[filename].content)
    for job_name, job in data["jobs"].items():
        cond = job.get("if", "")
        assert "github.repository_owner" not in cond, (
            f"{filename} job '{job_name}' must not contain a repository_owner guard"
        )


def test_fork_guard_omitted_when_repo_owner_empty(tmp_path: Path) -> None:
    """When auto-detection fails (non-github remote, no remote, etc.), no
    guard is rendered and workflows behave as they did pre-change."""
    cfg = Config.load(_minimal_config(tmp_path, _extra_for("ci-fix")))
    # cfg.repo_owner is "" by default — Config.load does not auto-detect.
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    for filename in _GUARDED_WORKFLOWS:
        data = yaml.safe_load(workflows[filename].content)
        for job_name, job in data["jobs"].items():
            assert "github.repository_owner" not in job.get("if", ""), (
                f"{filename} job '{job_name}' must not contain the guard "
                "when repo_owner is unset"
            )
    # ci-fix's pre-existing conclusion check must survive even without the guard
    ci_fix = yaml.safe_load(workflows["tend-ci-fix.yaml"].content)
    assert (
        ci_fix["jobs"]["fix-ci"]["if"]
        == "github.event.workflow_run.conclusion == 'failure'"
    )


@pytest.mark.parametrize(
    "workflow_name,job_name,user_if,extra_workflow_keys",
    [
        # Triage: the guard is the *only* job-level if; clobbering loses just it.
        (
            "triage",
            "triage",
            "github.event.issue.author_association != 'NONE'",
            {},
        ),
        # ci-fix: the rendered if is `<guard> && <conclusion-check>`. Clobbering
        # removes BOTH — so the workflow would also lose its "only run on
        # failure" gate. More interesting than triage because runtime semantics
        # change beyond just the fork guard.
        (
            "ci-fix",
            "fix-ci",
            "github.actor == 'tend-agent'",
            {"watched_workflows": ["ci"]},
        ),
    ],
)
def test_user_job_if_extra_replaces_fork_guard(
    tmp_path: Path,
    workflow_name: str,
    job_name: str,
    user_if: str,
    extra_workflow_keys: dict,
) -> None:
    """A user-supplied job-level `if:` replaces the rendered job-level if via
    RFC 7396 scalar replacement — this includes the fork guard *and* any other
    conditions tend composed with it (ci-fix's conclusion check, future
    combined ifs).

    Pins current behavior so a future merge-strategy change is a deliberate
    choice, not an accident. If we ever decide to compose user extras with
    the rendered conditions instead of letting them clobber, this test fails
    loudly and docs/tend.example.yaml should be updated alongside.
    """
    wf_block = {
        **extra_workflow_keys,
        "jobs": {job_name: {"if": user_if}},
    }
    extra = yaml.safe_dump({"workflows": {workflow_name: wf_block}})
    cfg = Config.load(_minimal_config(tmp_path, extra))
    cfg.repo_owner = "test-owner"
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows[f"tend-{workflow_name}.yaml"].content)
    rendered_if = data["jobs"][job_name]["if"]
    # User condition wins outright — no `&&`, no guard, no other conditions.
    assert rendered_if == user_if
    assert "github.repository_owner" not in rendered_if


@pytest.mark.parametrize("filename", _GUARDED_WORKFLOWS)
def test_fork_guard_rendered_shape_regtest(
    regtest: object, tmp_path: Path, filename: str
) -> None:
    """Snapshot the production rendered shape (with the guard line) for every
    fork-exposed workflow, so indentation or structural drift in the rendered
    `if:` line is caught — the `_minimal_config`-based regtests above only
    cover the no-guard fallback."""
    name = filename.removeprefix("tend-").removesuffix(".yaml")
    cfg = Config.load(_minimal_config(tmp_path, _extra_for(name)))
    cfg.repo_owner = "test-owner"
    wf = next(w for w in generate_all(cfg) if w.filename == filename)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pass-through extras (workflow_extra / jobs)
# ---------------------------------------------------------------------------


def test_deep_merge_rfc7396() -> None:
    """RFC 7396: mappings deep-merge, scalars/lists replace, None deletes."""
    base = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2]}
    override = {"b": {"c": 99, "x": 10}, "e": [3], "f": 4}
    assert _deep_merge(base, override) == {
        "a": 1,
        "b": {"c": 99, "d": 3, "x": 10},
        "e": [3],
        "f": 4,
    }
    # None deletes
    assert _deep_merge({"a": 1, "b": 2}, {"b": None}) == {"a": 1}


def test_job_extras_add_key(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          review:
            jobs:
              review:
                timeout-minutes: 240
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    assert data["jobs"]["review"]["timeout-minutes"] == 240
    # Original keys preserved
    assert data["jobs"]["review"]["runs-on"] == "ubuntu-24.04"


def test_job_extras_deep_merge_permissions(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          review:
            jobs:
              review:
                permissions:
                  packages: read
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    perms = data["jobs"]["review"]["permissions"]
    assert perms["contents"] == "write"
    assert perms["pull-requests"] == "write"
    assert perms["packages"] == "read"


def test_workflow_extras_add_env(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          review:
            workflow_extra:
              env:
                MY_VAR: hello
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    assert data["env"]["MY_VAR"] == "hello"
    # Other workflows unaffected
    triage = yaml.safe_load(workflows["tend-triage.yaml"].content)
    assert "env" not in triage


def test_mention_job_extras_target_specific_job(tmp_path: Path) -> None:
    """Multi-job workflow: extras target only the named job."""
    extra = dedent("""\
        workflows:
          mention:
            jobs:
              handle:
                timeout-minutes: 180
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-mention.yaml"].content)
    assert data["jobs"]["handle"]["timeout-minutes"] == 180
    assert "timeout-minutes" not in data["jobs"]["verify"]


def test_extras_preserve_header(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          review:
            jobs:
              review:
                timeout-minutes: 240
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    content = workflows["tend-review.yaml"].content
    assert content.startswith("# Generated by tend ")


def test_extras_produce_valid_yaml(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          review:
            jobs:
              review:
                timeout-minutes: 240
            workflow_extra:
              env:
                FOO: bar
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        data = yaml.safe_load(wf.content)
        assert isinstance(data, dict), f"{wf.filename} did not parse as dict"
        assert "jobs" in data, f"{wf.filename} missing jobs"


def test_no_extras_output_unchanged(tmp_path: Path) -> None:
    """Without extras, generate_all() output matches direct generator output."""
    cfg = Config.load(_minimal_config(tmp_path))
    via_all = {wf.filename: wf.content for wf in generate_all(cfg)}
    for name, gen_fn in GENERATORS.items():
        try:
            wf = gen_fn(cfg)
        except click.ClickException:
            continue  # ci-fix requires watched_workflows
        if wf.filename in via_all:
            assert via_all[wf.filename] == wf.content, (
                f"{wf.filename}: generate_all() changed output without extras"
            )


def test_job_extras_replace_if_for_skip_review_label(tmp_path: Path) -> None:
    """Override `if:` on the review job to skip PRs with a dismissal label.

    Documented in docs/tend.example.yaml and the install-tend skill as the
    canonical way to opt out of re-reviews after the initial pass, replacing
    post-regeneration patching scripts.
    """
    skip_if = (
        "github.event.pull_request.draft == false && "
        "!contains(github.event.pull_request.labels.*.name, 'tend:dismissed')"
    )
    extra = yaml.safe_dump(
        {"workflows": {"review": {"jobs": {"review": {"if": skip_if}}}}}
    )
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    assert data["jobs"]["review"]["if"] == skip_if
    # Other review-job keys are preserved (deep merge of the job mapping).
    assert data["jobs"]["review"]["runs-on"] == "ubuntu-24.04"
    assert "permissions" in data["jobs"]["review"]
    assert "steps" in data["jobs"]["review"]


def test_null_drops_top_level_key(tmp_path: Path) -> None:
    """YAML-native `null` in workflow_extra removes the targeted key under
    RFC 7396 Merge Patch semantics. The motivating case: keep nightly's
    `workflow_dispatch` trigger but drop the cron schedule."""
    extra = dedent("""\
        workflows:
          nightly:
            workflow_extra:
              on:
                schedule: null
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-nightly.yaml"].content)
    triggers = data["on"]
    assert "schedule" not in triggers
    assert "workflow_dispatch" in triggers


def test_null_drops_nested_key(tmp_path: Path) -> None:
    """`null` works at any depth inside a job override."""
    extra = dedent("""\
        workflows:
          review:
            jobs:
              review:
                permissions:
                  issues: null
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    perms = yaml.safe_load(workflows["tend-review.yaml"].content)["jobs"]["review"][
        "permissions"
    ]
    assert "issues" not in perms
    assert perms["contents"] == "write"


def test_null_drops_missing_key_is_noop(tmp_path: Path) -> None:
    """Deleting a key that doesn't exist is silently a no-op (RFC 7396)."""
    extra = dedent("""\
        workflows:
          review:
            workflow_extra:
              nonexistent: null
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    assert "nonexistent" not in data


def test_unknown_job_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    extra = dedent("""\
        workflows:
          review:
            jobs:
              nonexistent:
                timeout-minutes: 240
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    generate_all(cfg)
    captured = capsys.readouterr()
    assert "nonexistent" in captured.err


# ---------------------------------------------------------------------------
# Regtest snapshots — full YAML output for every workflow
# ---------------------------------------------------------------------------


def _extra_for(name: str) -> str:
    """Return extra config needed for a specific generator (e.g. ci-fix)."""
    if name == "ci-fix":
        return 'workflows:\n  ci-fix:\n    watched_workflows: ["ci"]\n'
    return ""


@pytest.mark.parametrize("name", GENERATORS)
def test_workflow_minimal_regtest(regtest: object, tmp_path: Path, name: str) -> None:
    """Snapshot each workflow's full YAML with minimal config."""
    cfg = Config.load(_minimal_config(tmp_path, _extra_for(name)))
    wf = GENERATORS[name](cfg)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", GENERATORS)
def test_workflow_with_setup_regtest(
    regtest: object, tmp_path: Path, name: str
) -> None:
    """Snapshot each workflow's full YAML with a setup step."""
    extra = "setup:\n  - uses: astral-sh/setup-uv@v6\n"
    extra_cfg = _extra_for(name)
    if extra_cfg:
        extra += extra_cfg
    cfg = Config.load(_minimal_config(tmp_path, extra))
    wf = GENERATORS[name](cfg)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["review", "mention"])
def test_workflow_with_local_setup_regtest(
    regtest: object, tmp_path: Path, name: str
) -> None:
    """Snapshot the two workflows that swap the workspace tree after `setup:`,
    with a local composite in it — locks the restore step the POST chain needs
    (actions/runner#2816)."""
    extra = "setup:\n  - uses: ./.github/actions/tend-setup\n"
    cfg = Config.load(_minimal_config(tmp_path, extra))
    wf = GENERATORS[name](cfg)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]


def test_sandbox_levers_regtest(regtest: object, tmp_path: Path) -> None:
    """Snapshot the rendered agent step with all three sandbox levers set, to
    lock the block-scalar shape threaded to the composite action."""
    extra = dedent("""\
        sandbox_path:
          - ~/.cargo/bin
          - /opt/tools/bin
        sandbox_env:
          RUST_BACKTRACE: "1"
          CARGO_TERM_COLOR: always
        sandbox_setup:
          - rustup component add clippy
          - cargo fetch --locked
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    print(generate_mention(cfg).content, end="", file=regtest)  # type: ignore[arg-type]


def test_extras_apply_path_regtest(regtest: object, tmp_path: Path) -> None:
    """Snapshot the workflow with both `workflow_extra` and per-job overrides
    applied — exercises the `_apply_extras` round-trip path that the other
    regtests skip. Catches renderer drift (lost quoting, indent changes,
    duplicated headers, key-order churn) on any change to the ruamel.yaml
    dumper config or `_deep_merge` semantics."""
    extra = dedent("""\
        workflows:
          review:
            workflow_extra:
              env:
                FOO: bar
            jobs:
              review:
                timeout-minutes: 240
                permissions:
                  packages: read
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    print(workflows["tend-review.yaml"].content, end="", file=regtest)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Codex harness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", GENERATORS)
def test_workflow_minimal_codex_regtest(
    regtest: object, tmp_path: Path, name: str
) -> None:
    """Snapshot the Codex-harness variant of every workflow."""
    extra = "harness: codex\n" + _extra_for(name)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    wf = GENERATORS[name](cfg)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Install test workflow (not in GENERATORS — only generated via --with-install-test)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_install_test_workflow_regtest(
    regtest: object, tmp_path: Path, harness: str
) -> None:
    """Snapshot the install-test workflow YAML for every harness."""
    extra = f"harness: {harness}\n" if harness != "claude" else ""
    cfg = Config.load(_minimal_config(tmp_path, extra))
    wf = generate_install_test(cfg)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]


def test_install_test_honors_workflow_extras(tmp_path: Path) -> None:
    """install-test goes through `_apply_extras`, so `workflow_extra` and
    per-job overrides from .config/tend.yaml take effect (e.g. injecting
    proxy env vars or pinning runs-on for a self-hosted runner)."""
    extra = dedent("""\
        workflows:
          install-test:
            workflow_extra:
              env:
                HTTPS_PROXY: http://proxy.corp:8080
            jobs:
              install-test:
                runs-on: ubuntu-22.04-large
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg, with_install_test=True)}
    data = yaml.safe_load(workflows["tend-install-test.yaml"].content)
    assert data["env"]["HTTPS_PROXY"] == "http://proxy.corp:8080"
    assert data["jobs"]["install-test"]["runs-on"] == "ubuntu-22.04-large"


def test_codex_action_ref(tmp_path: Path) -> None:
    """Codex workflows reference max-sixty/tend/codex@<release tag>."""
    cfg = Config.load(_minimal_config(tmp_path, "harness: codex"))
    for wf in generate_all(cfg):
        assert f"max-sixty/tend/codex@{ACTION_VERSION}" in wf.content, (
            f"{wf.filename} missing codex action ref"
        )
        assert f"max-sixty/tend/claude@{ACTION_VERSION}" not in wf.content, (
            f"{wf.filename} should not reference the claude action ref"
        )


def test_codex_workflows_use_openai_secrets_not_claude(tmp_path: Path) -> None:
    """Codex agent step references OPENAI_API_KEY, not Claude or auth.json."""
    cfg = Config.load(_minimal_config(tmp_path, "harness: codex"))
    for wf in generate_all(cfg):
        assert "openai_api_key: ${{ secrets.OPENAI_API_KEY }}" in wf.content, (
            f"{wf.filename} missing openai_api_key input"
        )
        assert "codex_auth_json" not in wf.content, (
            f"{wf.filename} should not reference codex_auth_json"
        )
        assert "claude_code_oauth_token" not in wf.content, (
            f"{wf.filename} should not reference claude_code_oauth_token under codex"
        )


def test_codex_effort_only_when_set(tmp_path: Path) -> None:
    """effort: renders only when configured."""
    cfg_default = Config.load(_minimal_config(tmp_path, "harness: codex"))
    for wf in generate_all(cfg_default):
        assert "effort:" not in wf.content, (
            f"{wf.filename} should omit effort when unset"
        )

    cfg_with_effort = Config.load(
        _minimal_config(tmp_path, "harness: codex\neffort: high")
    )
    for wf in generate_all(cfg_with_effort):
        assert "effort: high" in wf.content, f"{wf.filename} missing effort: high"


def test_codex_default_model(tmp_path: Path) -> None:
    """Engine = codex without explicit model picks gpt-5.5."""
    cfg = Config.load(_minimal_config(tmp_path, "harness: codex"))
    assert cfg.model == "gpt-5.5"
    wf = next(w for w in generate_all(cfg) if w.filename == "tend-triage.yaml")
    assert "model: gpt-5.5" in wf.content


def test_unknown_engine_rejected(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="harness 'gpt' is not recognized"):
        Config.load(_minimal_config(tmp_path, "harness: gpt"))


# ---------------------------------------------------------------------------
# Per-workflow harness override
# ---------------------------------------------------------------------------


def test_per_workflow_harness_override_targets_only_named_workflow(
    tmp_path: Path,
) -> None:
    """`workflows.<name>.harness` flips the action ref for that workflow
    only; sibling workflows keep the top-level harness. This is what lets
    an adopter trial codex on nightly without flipping their PR-review
    workflow."""
    extra = dedent("""\
        workflows:
          nightly:
            harness: codex
            model: gpt-5.5
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}

    nightly = workflows["tend-nightly.yaml"]
    assert f"max-sixty/tend/codex@{ACTION_VERSION}" in nightly.content
    assert f"max-sixty/tend/claude@{ACTION_VERSION}" not in nightly.content
    # The override carries the harness's own secret shape, not the top level's.
    assert "openai_api_key" in nightly.content
    assert "claude_code_oauth_token" not in nightly.content

    # Sibling workflows still use the top-level claude harness.
    review = workflows["tend-review.yaml"]
    assert f"max-sixty/tend/claude@{ACTION_VERSION}" in review.content
    assert f"max-sixty/tend/codex@{ACTION_VERSION}" not in review.content


def test_per_workflow_harness_unknown_rejected(tmp_path: Path) -> None:
    extra = dedent("""\
        workflows:
          nightly:
            harness: gpt
    """)
    with pytest.raises(
        click.ClickException, match="workflows.nightly.harness 'gpt' is not recognized"
    ):
        Config.load(_minimal_config(tmp_path, extra))


def test_per_workflow_harness_incompatible_model_rejected_codex_target(
    tmp_path: Path,
) -> None:
    """codex (gpt-5.5) → claude per-workflow: top-level model 'gpt-5.5' isn't
    in claude's allowlist. Fail at config load."""
    extra = dedent("""\
        harness: codex
        model: gpt-5.5
        workflows:
          nightly:
            harness: claude
    """)
    with pytest.raises(
        click.ClickException,
        match=r"workflows.nightly harness 'claude' is incompatible with model 'gpt-5.5'",
    ):
        Config.load(_minimal_config(tmp_path, extra))


def test_per_workflow_harness_incompatible_model_rejected_codex_source(
    tmp_path: Path,
) -> None:
    """Symmetric case: claude (opus) → codex per-workflow without a model
    override. codex doesn't accept 'opus' but has no allowlist, so the
    cross-family-source check catches it instead of the target-allowlist
    check. Reviewer-flagged asymmetry in #612.

    Without this guard, the renderer would emit `model: opus` on a codex
    action step that codex would reject at runtime."""
    extra = dedent("""\
        harness: claude
        workflows:
          nightly:
            harness: codex
    """)
    with pytest.raises(
        click.ClickException,
        match=r"workflows.nightly crosses harness families \(claude → codex\)",
    ):
        Config.load(_minimal_config(tmp_path, extra))


def test_per_workflow_model_override_unblocks_cross_family(tmp_path: Path) -> None:
    """Cross-family override succeeds when paired with a per-workflow model."""
    extra = dedent("""\
        harness: claude
        workflows:
          nightly:
            harness: codex
            model: gpt-5.5
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    nightly = workflows["tend-nightly.yaml"]
    assert f"max-sixty/tend/codex@{ACTION_VERSION}" in nightly.content
    assert "model: gpt-5.5" in nightly.content
    # Sibling workflows still on claude with opus
    review = workflows["tend-review.yaml"]
    assert f"max-sixty/tend/claude@{ACTION_VERSION}" in review.content
    assert "model: opus" in review.content


def test_per_workflow_model_typo_without_harness_rejected(tmp_path: Path) -> None:
    """Reviewer-flagged gap (#612): per-workflow `model:` override WITHOUT
    a `harness:` change must still be validated against the top-level
    harness's allowlist. Previously skipped because both checks gated on
    `wf_harness is not None`."""
    extra = dedent("""\
        workflows:
          nightly:
            model: opus-99
    """)
    with pytest.raises(
        click.ClickException,
        match=r"workflows.nightly harness 'claude' is incompatible with model 'opus-99'",
    ):
        Config.load(_minimal_config(tmp_path, extra))


def test_per_workflow_model_only_override_valid(tmp_path: Path) -> None:
    """Per-workflow `model:` override (no harness change) to a valid model
    in the top-level harness's allowlist loads cleanly and renders."""
    extra = dedent("""\
        workflows:
          nightly:
            model: haiku
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    nightly = workflows["tend-nightly.yaml"]
    assert "model: haiku" in nightly.content
    review = workflows["tend-review.yaml"]
    assert "model: opus" in review.content


def test_codex_model_unrestricted(tmp_path: Path) -> None:
    """Codex model strings pass through unvalidated.

    Codex's catalog churns (gpt-5.1-codex was current at harness bring-up;
    deprecated by the next month). An allowlist would silently lock adopters
    out of newer models. We accept any string and let `codex exec` error at
    runtime if it's wrong.
    """
    cfg = Config.load(_minimal_config(tmp_path, "harness: codex\nmodel: gpt-99-future"))
    assert cfg.model == "gpt-99-future"


def test_unknown_claude_model_rejected(tmp_path: Path) -> None:
    """Claude's model set is small and stable; typos fail at config load."""
    with pytest.raises(
        click.ClickException, match="not recognized for harness 'claude'"
    ):
        Config.load(_minimal_config(tmp_path, "model: opus-3"))


def test_effort_rejected_for_claude(tmp_path: Path) -> None:
    """effort is Codex-only — Claude has no reasoning-effort knob."""
    with pytest.raises(
        click.ClickException, match="effort is only valid for harness = 'codex'"
    ):
        Config.load(_minimal_config(tmp_path, "effort: high"))


def test_unknown_effort_rejected(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="effort 'turbo' is not recognized"):
        Config.load(_minimal_config(tmp_path, "harness: codex\neffort: turbo"))
