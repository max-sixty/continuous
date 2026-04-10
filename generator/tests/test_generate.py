"""Smoke tests for workflow generation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml
import click
from click.testing import CliRunner

from tend.cli import main
from tend.config import Config
from tend.workflows import GENERATORS, _deep_merge, generate_all, generate_mention


def _minimal_config(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / ".config" / "tend.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'bot_name = "test-bot"\n{extra}')
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
    cfg = Config.load(_minimal_config(tmp_path, "[workflows.weekly]\nenabled = false"))
    workflows = generate_all(cfg)
    names = {wf.filename for wf in workflows}
    assert "tend-weekly.yaml" not in names
    assert len(workflows) == 6


def test_setup_steps_rendered(tmp_path: Path) -> None:
    extra = dedent("""\
        setup = [
          {uses = "./.github/actions/my-setup"},
          {run = "echo FOO=bar >> $GITHUB_ENV"},
        ]
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert "./.github/actions/my-setup" in wf.content, (
            f"{wf.filename} missing uses step"
        )
        assert "echo FOO=bar >> $GITHUB_ENV" in wf.content, (
            f"{wf.filename} missing run step"
        )


def test_empty_setup_no_blank_lines(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_config(tmp_path))
    for wf in generate_all(cfg):
        assert "\n\n\n" not in wf.content, f"{wf.filename} has triple blank lines"


def test_custom_secrets(tmp_path: Path) -> None:
    extra = dedent("""\
        [secrets]
        bot_token = "MY_BOT_PAT"
        claude_token = "MY_CLAUDE"
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert "MY_BOT_PAT" in wf.content, f"{wf.filename} missing custom bot token"
        assert "MY_CLAUDE" in wf.content, f"{wf.filename} missing custom claude token"


def test_custom_prompt(tmp_path: Path) -> None:
    extra = dedent("""\
        [workflows.triage]
        prompt = "Custom triage: {issue_number}"
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    triage = workflows["tend-triage.yaml"]
    assert "Custom triage:" in triage.content


def test_watched_workflows(tmp_path: Path) -> None:
    extra = dedent("""\
        [workflows.ci-fix]
        watched_workflows = ["build", "test", "lint"]
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
        [workflows.ci-fix]
        watched_workflows = ["ci"]
        branches = ["main", "release"]
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


def test_setup_after_checkout_in_review(tmp_path: Path) -> None:
    """Setup steps must run after checkout, not before."""
    extra = 'setup = [{uses = "./.github/actions/my-setup"}]'
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    review = workflows["tend-review.yaml"]
    checkout_idx = review.content.index("actions/checkout@v6")
    setup_idx = review.content.index("./.github/actions/my-setup")
    assert setup_idx > checkout_idx, "Setup must come after checkout"


def test_setup_raw_yaml_injected(tmp_path: Path) -> None:
    extra = dedent('''\
        setup = [
          {raw = """
        - uses: Swatinem/rust-cache@v2
          with:
            save-if: false
        - run: cargo binstall cargo-insta --no-confirm
          shell: bash
        """},
        ]
    ''')
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        data = yaml.safe_load(wf.content)
        assert isinstance(data, dict), f"{wf.filename} did not parse as valid YAML"
        assert "Swatinem/rust-cache@v2" in wf.content, (
            f"{wf.filename} missing raw uses step"
        )
        assert "save-if: false" in wf.content, f"{wf.filename} missing with parameter"
        assert "cargo binstall" in wf.content, f"{wf.filename} missing raw run step"


def test_setup_raw_interleaved_with_steps(tmp_path: Path) -> None:
    extra = dedent('''\
        setup = [
          {uses = "./.github/actions/my-setup"},
          {raw = """
        - uses: Swatinem/rust-cache@v2
          with:
            save-if: false
        """},
          {run = "echo FOO=bar >> $GITHUB_ENV"},
        ]
    ''')
    cfg = Config.load(_minimal_config(tmp_path, extra))
    for wf in generate_all(cfg):
        assert "./.github/actions/my-setup" in wf.content
        assert "Swatinem/rust-cache@v2" in wf.content
        assert "save-if: false" in wf.content
        assert "echo FOO=bar" in wf.content
        # Order preserved: uses, raw, run
        uses_idx = wf.content.index("./.github/actions/my-setup")
        raw_idx = wf.content.index("Swatinem/rust-cache@v2")
        run_idx = wf.content.index("echo FOO=bar")
        assert uses_idx < raw_idx < run_idx, f"{wf.filename}: wrong order"


def test_mention_handles_pull_request_review(tmp_path: Path) -> None:
    """pull_request_review (submitted) must be covered by tend-mention so the bot
    responds when a reviewer submits a formal review on an engaged PR."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)

    # Event trigger present
    assert "pull_request_review" in data[True], (
        "tend-mention must listen for pull_request_review events"
    )
    assert data[True]["pull_request_review"] == {"types": ["submitted"]}

    # Verify job filters out fork PRs for review events — secrets are
    # unavailable there. The notifications workflow polls for these.
    verify_if = data["jobs"]["verify"]["if"]
    assert "pull_request_review" in verify_if
    assert "issue_comment" in verify_if
    assert "pull_request.head.repo.full_name" in verify_if

    # Handle job checks out PR branch for this event
    handle_steps = data["jobs"]["handle"]["steps"]
    checkout_step = next(
        s for s in handle_steps if s.get("name") == "Check out PR branch"
    )
    assert "pull_request_review" in checkout_step["if"]

    # Prompt includes review-specific branches
    tend_step = next(
        s for s in handle_steps if s.get("uses", "").startswith("max-sixty/tend@")
    )
    prompt = tend_step["with"]["prompt"]
    assert "github.event.review.html_url" in prompt
    assert "github.event.review.body" in prompt


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
    assert data[True]["pull_request_review_comment"] == {"types": ["edited"]}, (
        "pull_request_review_comment must subscribe to ['edited'] only — see "
        "the trigger comment in generate_mention for the dedup rationale"
    )


def test_mention_verify_detects_inline_mentions_on_review(tmp_path: Path) -> None:
    """For pull_request_review events, verify must fetch the review's inline
    comments and grep their bodies for the bot mention.

    The pull_request_review event payload exposes review.body but NOT the
    bodies of the inline comments attached to that review. So a first-contact
    "@bot" mention written *inside* an inline review comment is invisible to
    the COMMENT_BODY check (which only sees review.body for review events) and
    to the engagement check (which only fires when the bot has prior
    engagement on the PR). Without this fetch, such mentions would be silently
    dropped on PRs where the bot has no prior engagement.

    Today the gap is masked stochastically by the pull_request_review_comment
    sibling event firing in parallel and exposing comment.body. Once we stop
    subscribing to `created` for that event (see
    test_mention_review_comment_listens_only_for_edits), the masking goes away
    and verify must detect the mention via the API."""
    cfg = Config.load(_minimal_config(tmp_path))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    data = yaml.safe_load(mention.content)
    check_step = next(
        s for s in data["jobs"]["verify"]["steps"] if s.get("id") == "check"
    )

    # The fetch must target the specific review's inline comments by review_id
    assert "/reviews/$REVIEW_ID/comments" in check_step["run"], (
        "verify must fetch inline comments for pull_request_review events via "
        "the /pulls/{n}/reviews/{review_id}/comments endpoint"
    )
    # And grep their bodies for the bot mention (test-bot is the bot_name in
    # _minimal_config). grep -qF means fixed-string, quiet — same shape as the
    # other mention checks in this script.
    assert "grep -qF '@test-bot'" in check_step["run"], (
        "verify must grep the fetched inline comment bodies for the bot mention"
    )

    # The fetch must be gated on event_name == 'pull_request_review' so it
    # doesn't fire for issue_comment / issues / pull_request_review_comment.
    assert '[ "$EVENT_NAME" = "pull_request_review" ]' in check_step["run"], (
        "the inline-mention fetch must only run for pull_request_review events"
    )

    # REVIEW_ID env var must be wired from the event payload
    assert check_step["env"]["REVIEW_ID"] == "${{ github.event.review.id }}", (
        "REVIEW_ID env var must be set from github.event.review.id so the "
        "fetch can target the right review"
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


def test_setup_after_pr_checkout_in_mention(tmp_path: Path) -> None:
    """Setup steps must run after PR checkout, not before."""
    extra = 'setup = [{uses = "./.github/actions/my-setup"}]'
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    mention = workflows["tend-mention.yaml"]
    # Setup should come after "Check out PR branch"
    checkout_idx = mention.content.index("Check out PR branch")
    setup_idx = mention.content.index("./.github/actions/my-setup")
    assert setup_idx > checkout_idx, "Setup must come after PR checkout"


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
    tend_idx = mention.content.index("max-sixty/tend@v1")
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
        if s.get("uses", "").startswith("max-sixty/tend@")
    )
    prompt = tend_step["with"]["prompt"]
    # The delay text must be inside a format() conditional, not hardcoded
    assert "format(" in prompt, "delay preamble must use conditional format()"
    # "Before acting" must always appear (it's the unconditional part)
    assert "Before acting" in prompt


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
        [workflows.review.jobs.review]
        timeout-minutes = 240
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-review.yaml"].content)
    assert data["jobs"]["review"]["timeout-minutes"] == 240
    # Original keys preserved
    assert data["jobs"]["review"]["runs-on"] == "ubuntu-24.04"


def test_job_extras_deep_merge_permissions(tmp_path: Path) -> None:
    extra = dedent("""\
        [workflows.review.jobs.review.permissions]
        packages = "read"
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
        [workflows.review.workflow_extra.env]
        MY_VAR = "hello"
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
        [workflows.mention.jobs.handle]
        timeout-minutes = 180
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    data = yaml.safe_load(workflows["tend-mention.yaml"].content)
    assert data["jobs"]["handle"]["timeout-minutes"] == 180
    assert "timeout-minutes" not in data["jobs"]["verify"]


def test_extras_preserve_header(tmp_path: Path) -> None:
    extra = dedent("""\
        [workflows.review.jobs.review]
        timeout-minutes = 240
    """)
    cfg = Config.load(_minimal_config(tmp_path, extra))
    workflows = {wf.filename: wf for wf in generate_all(cfg)}
    content = workflows["tend-review.yaml"].content
    assert content.startswith("# Generated by tend.")


def test_extras_produce_valid_yaml(tmp_path: Path) -> None:
    extra = dedent("""\
        [workflows.review.jobs.review]
        timeout-minutes = 240
        [workflows.review.workflow_extra.env]
        FOO = "bar"
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


def test_unknown_job_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    extra = dedent("""\
        [workflows.review.jobs.nonexistent]
        timeout-minutes = 240
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
        return '[workflows.ci-fix]\nwatched_workflows = ["ci"]'
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
    extra = 'setup = [{uses = "astral-sh/setup-uv@v6"}]'
    extra_cfg = _extra_for(name)
    if extra_cfg:
        extra += "\n" + extra_cfg
    cfg = Config.load(_minimal_config(tmp_path, extra))
    wf = GENERATORS[name](cfg)
    print(wf.content, end="", file=regtest)  # type: ignore[arg-type]
