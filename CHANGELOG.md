# Changelog

Notable changes per release. The `## X.Y.Z` section for each version is
published verbatim as that version's GitHub Release notes
(`.github/workflows/pypi-release.yaml`). Newest first. Releases before
0.1.1 predate this changelog; see the compare views at
https://github.com/max-sixty/tend/compare for their history.

## 0.1.14

### Improved

- **`tend check` sweeps credential-holding environments rather than secret-holding ones**, and the check is renamed `credential-environments`. An environment holds a credential if it stores a secret *or* a job requesting `id-token: write` deploys to it, so a repo publishing through a trusted publisher no longer passes unexamined. A ref policy no longer counts as a gate under `release: published`, `repository_dispatch`, or `workflow_dispatch` with inputs, where a write-scoped actor supplies the run's payload and fires it at an admitted ref; only a required reviewer covers those. `id-token: write` outside any environment is reported on its own line. ([#815](https://github.com/max-sixty/tend/pull/815))

### Fixed

- **Generated workflows name the environment as `{name: tend, deployment: false}`, so a job no longer files a deployment record.** GitHub files one for every job that names an environment, against the run's own head — under `pull_request_target` that is the PR's head, so every push to every PR added a `<bot> deployed to tend` line to its timeline. The deployment-branch policy that gates the operational secrets is unaffected. `tend check` grows an `environment-deployments` check that fails a job naming the environment without it. ([#852](https://github.com/max-sixty/tend/pull/852), [#853](https://github.com/max-sixty/tend/pull/853))
- **`tend-mention` counts the bot's engagement outside `jq`.** `gh` applies `--jq` once per page, so a reduction inside a `--paginate`d call emitted one count per page; past 100 reviews or comments on one thread the variable held a multi-line value and the numeric guard below it errored, so the step fell through to `should_run=false`. The bot went silent on exactly the threads it had engaged with most, with nothing going red. ([#840](https://github.com/max-sixty/tend/pull/840))
- **The `review` skill ignores synthetic reply containers when reading the bot's own review records.** Replying to a review thread wraps the reply in a zero-body `COMMENTED` review anchored at the then-current HEAD, so all three guards derived from those records counted it: `LAST_REVIEW_SHA` advanced past commits nothing had reviewed, the already-posted check discarded a review the run had already formed, and the 422 recovery pointed at an unrelated reply. A record now counts only if it carries a body, owns a top-level inline comment, or is `APPROVED`. ([#835](https://github.com/max-sixty/tend/pull/835))
- **The review's second pass reaches a code-review skill again.** The built-in `/code-review` carries `disable-model-invocation`, so invoking it returned a tool-use error and the pass silently degraded to the manual review alone. The prompt is now a tend-owned `/tend-ci-runner:code-review`, which the Codex harness resolves too — it previously skipped the second pass entirely. ([#819](https://github.com/max-sixty/tend/pull/819))
- **`mark-notification-read` tolerates a transient run-metadata fetch failure.** The unguarded `gh api` call ran under `set -eo pipefail` in a step gated on `if: success()`, so a dropped request turned an otherwise successful run red. It now skips the cycle and leaves the thread to the scheduled `tend-notifications` poll. ([#843](https://github.com/max-sixty/tend/pull/843))
- **A `tend-outage` row names the trigger it points at.** `repository_dispatch` — the path every relayed review event takes — had no branch and recorded `N/A`, `workflow_run` discarded the id of the run it was dispatched to fix, and an absent field rendered as `#null`. ([#823](https://github.com/max-sixty/tend/pull/823))
- **The triage skill's `gh pr create` recipes substitute the issue number into the PR body.** Both blocks ended with a literal `#<issue-number>` while the commit message and title on the surrounding lines used `$ARGUMENTS`, so a run following them verbatim shipped the placeholder and left the issue unlinked. ([#844](https://github.com/max-sixty/tend/pull/844))

### Internal

- `claude-smoke` and the `tend-manual` environment it required are removed. It ran four times, all while the headless proxy harness was under construction, and CI's `test-proxy` job covers its deterministic half at the production-pinned mitmproxy. ([#820](https://github.com/max-sixty/tend/pull/820))
- The weekly bump rule covers third-party `uses:` refs. Dependabot has never run on this repo, and it reaches neither the composite actions nor `macros.yaml.j2`, which renders `actions/checkout` into every adopter's workflows. ([#814](https://github.com/max-sixty/tend/pull/814))

## 0.1.13

### Improved

- **The operational secrets move into a gated `tend` deployment environment.** Generated workflows name `environment: tend`, so `TEND_BOT_TOKEN` and the model credential are released only to a job whose ref the environment's deployment-branch policy admits, where before any workflow on any branch could read them. `tend check` grows an `environment` check (the policy must admit exactly the branches whose protection passes) and a `secret-environments` check (any other secret-holding environment needs a gate of its own), and `check --fix` creates the environment. Migration is per repo and manual, since a secret can't be read back: `tend check --fix`, then `gh secret set <NAME> --repo <repo> --env tend` for each operational secret, then delete the repo-level copies. ([#810](https://github.com/max-sixty/tend/pull/810))
- **`tend-mention` handles review events through a secretless relay.** A review or review-comment event runs on `refs/pull/N/merge`, a ref no branch policy can admit, so the triggered job now holds no secrets and re-posts only identifiers (`{kind, pr, id}`) as a `repository_dispatch`; the dispatched job re-reads the record from the API and applies the same engagement checks, so a forged dispatch gains nothing. ([#810](https://github.com/max-sixty/tend/pull/810))
- **`tend check` verifies that the bot cannot bypass the merge restriction.** Each `update` rule is traced to its ruleset and every bypass actor must outrank write — unknown role IDs fail closed, and a `bypass_actors` list GitHub withholds reports as a skip rather than a pass. The bot's own level is read from the `permissions` booleans instead of the legacy `.permission` string, which reports a maintain-role collaborator as `"write"`, and the preflight step re-proves the restriction as the bot on every run via `current_user_can_bypass`. ([#795](https://github.com/max-sixty/tend/pull/795))
- **The `claude-interactive` harness is removed**, leaving `claude` and `codex`. It existed to sidestep a metering change Anthropic paused and has not resumed, and the default `claude` harness now runs the same binary headless, so nothing selected it; `harness: claude-interactive` no longer validates. ([#804](https://github.com/max-sixty/tend/pull/804))

### Fixed

- **`tend-review` runs the adopter's `setup:` steps on the base tree, not the PR's.** Setup executes as the runner user, which holds sudo and the checkout PAT, so a fork's build backend or added dependencies ran with that access ahead of the sandbox; review now checks out the base tree, runs `setup:`, then lands the PR's tree with `clean: false`. Setup that must see the PR's own manifests belongs in `sandbox_setup:`. Adopters with no `setup:` generate byte-identical workflows. ([#806](https://github.com/max-sixty/tend/pull/806))
- **tend's uv no longer shadows the adopter's.** The proxy gets a pinned uv (new `uv_version` input) installed into `$RUNNER_TEMP` and addressed absolutely, and the sandbox step installs the Claude binary alone; skills needing `uvx tend@...` go through a wrapper that installs into `$HOME/.tend-uv` on first use. Before this an unpinned uv landed ahead of every PATH entry derived from the runner, so the process holding the PAT and the model credential started from whatever version happened to be there. ([#807](https://github.com/max-sixty/tend/pull/807))
- **The proxy readiness check waits for the port to accept a connection.** mitmdump writes its CA certificate before it binds the port and before it loads the addon, so the previous check could report the proxy up and launch the agent against a process that had already exited. ([#811](https://github.com/max-sixty/tend/pull/811))
- **A mid-flight check rollup no longer holds back the bot's approval.** The pre-APPROVE guard now separates a settled red from a `FAILURE` alongside checks still in flight — a cancellation cascade makes an `if: always()` merge-gate check resolve to `FAILURE` rather than `cancelled` — and polls to settlement before deciding. On a genuine terminal red with no prior substantive review it posts a brief COMMENT recording why approval is held. ([#798](https://github.com/max-sixty/tend/pull/798))

### Documentation

- **Corrected stale `claude-code-action` attributions** in the integration-test recipe and the `running-in-ci` skill. ([#793](https://github.com/max-sixty/tend/pull/793))

### Internal

- `review-reviewers.yaml` runs the default `claude` harness on a current pin. It was the last caller of `claude-interactive` anywhere, left at `0.1.9` by a rollback that only regenerated the generated workflows. ([#803](https://github.com/max-sixty/tend/pull/803))
- The weekly run gains a bump rule for the `mitmproxy_version` and `uv_version` pins, which dependabot can't reach (it updates `uses:` refs, never a `default:` under `inputs:`), and CI gains a `test-proxy` step that installs uv through `install-proxy-uv.sh` and starts mitmdump through `setup-sandbox.sh`. ([#811](https://github.com/max-sixty/tend/pull/811))
- Two `review-reviewers` Non-issues entries: a silent `tend-review` on a clean draft PR, and the `tend-triage` no-op when the bot's own tracking issue is created. ([#787](https://github.com/max-sixty/tend/pull/787), [#802](https://github.com/max-sixty/tend/pull/802))

## 0.1.12

### Improved

- **The default harness timeout rises from 3 hours (10800s) to 5h50m (21000s)** in both Claude harnesses (`claude/action.yaml` headless, `claude-interactive/action.yaml` PTY), leaving a 10-minute buffer under GitHub Actions' hard 6-hour job cap instead of cutting long sessions off at 3 hours. ([#790](https://github.com/max-sixty/tend/pull/790))

### Fixed

- **The bot no longer asks an upstream maintainer to do verification work it couldn't do itself.** When a check needs hardware or an environment CI doesn't have, the `running-in-ci` skill now escalates in order — do it yourself, add the capability to your own repo, ask a contributor, ask your own maintainer — and never hands the ask outward to someone reviewing the bot's change as a favor. ([#789](https://github.com/max-sixty/tend/pull/789))
- **`list-recent-runs.sh` fails loud on a transient `gh` API error instead of silently reporting zero runs.** A dropped `gh workflow list`/`gh run list` call previously read as a false all-clear that permanently skipped that window; it now retries with backoff and exits non-zero if every attempt fails. ([#784](https://github.com/max-sixty/tend/pull/784))
- **The notifications workflow tolerates a transient non-JSON response from the GitHub API** instead of failing the step outright. ([#780](https://github.com/max-sixty/tend/pull/780))

### Internal

- Bumped pinned `claude_version` to 2.1.220 in both Claude harnesses. ([#794](https://github.com/max-sixty/tend/pull/794))
- Bumped pinned `claude_version` to 2.1.215. ([#782](https://github.com/max-sixty/tend/pull/782))

## 0.1.11

### Improved

- **Adopters get levers to reach inside the Claude-family sandbox before the agent launches.** New top-level `.config/tend.yaml` keys — `sandbox_path` (dirs prepended to the sandbox `PATH`), `sandbox_env` (extra launch-env vars, reserved keys rejected), and `sandbox_setup` (commands run as the sandbox user before the agent starts) — cover cases runner-side `setup:` can't reach, e.g. `sandbox_path: ["~/.cargo/bin"]` for a cargo toolchain installed off the sandbox's fixed `PATH`. Claude-only; Codex already runs `setup:` directly on the runner. ([#768](https://github.com/max-sixty/tend/pull/768))
- **The sandbox `PATH` is derived from the runner's own `PATH`.** `setup-sandbox.sh` now translates each runner-home toolchain dir (`.cargo/bin`, `.dotnet/tools`, `composer/vendor/bin`, …) to its sandbox-home sibling and keeps it only if it exists and the sandbox UID can traverse it, replacing a hardcoded base `PATH` — so `/etc/skel`-seeded toolchains reach the agent automatically, with no per-language allowlist to grow. ([#767](https://github.com/max-sixty/tend/pull/767))
- **The bot may contribute to other repos on explicit invitation.** The `running-in-ci` skill's scope restriction now carves out an exception when a maintainer of the target repo asks for the contribution in-thread (or its published contributing policy welcomes it) and the contribution serves the bot's home repo — e.g. upstreaming a fix for a dependency bug it's working around. Inferred welcome (agent signals, an open "help wanted" label) isn't enough; the unsolicited-contribution default still holds otherwise. ([#770](https://github.com/max-sixty/tend/pull/770))

### Fixed

- **`tend-mention` skips the no-op session it used to spin up on the bot's own comments.** The bot's own PAT-based account isn't caught by the existing GitHub-App/Bot-account check, so its comments (e.g. gist-link updates on its own tracking issue) fell through to the engagement heuristics and spun up a session the self-loop guard could only exit after the fact. ([#751](https://github.com/max-sixty/tend/pull/751))
- **The bot now responds to actionable content in a review it left on its own PR**, instead of treating every self-authored review as a self-loop to exit silently — only a plain approval or a review between other participants still exits without acting. ([#762](https://github.com/max-sixty/tend/pull/762), [#775](https://github.com/max-sixty/tend/pull/775))
- **Duplicate `tend-outage` issues from a wide, concurrent-leg outage now self-heal.** When several matrix legs fail within the same jitter window, `report-failure.sh` settles briefly after creating an issue, lists every open `tend-outage` issue, keeps the lowest-numbered, and closes the rest as duplicates — convergent even when several legs race the same reconciliation. ([#744](https://github.com/max-sixty/tend/pull/744))
- **`list-recent-runs.sh` recovers completions from a dropped cron tick**, not just a delayed one, by anchoring its window to the previous *actually completed* run instead of assuming every scheduled tick fired — a dropped tick previously left that hour's runs unaudited with no visible error. ([#753](https://github.com/max-sixty/tend/pull/753))
- **The bot no longer appends a self-authored attribution sign-off** to PR/issue bodies or comments — the bot account already conveys authorship, and the harness action adds any needed footer automatically. ([#773](https://github.com/max-sixty/tend/pull/773))

### Documentation

- **The `weekly` workflow's example config now says it approves, not auto-merges, dependency updates** — a maintainer merges; the bot never does. ([#776](https://github.com/max-sixty/tend/pull/776))
- **Fixed the Codex session-log jq recipe for message content items**, which pointed at a stale `.payload.content[].input_text.text` path instead of selecting the `input_text`-typed item. ([#771](https://github.com/max-sixty/tend/pull/771))

### Internal

- Bumped pinned `claude_version` to 2.1.207. ([#774](https://github.com/max-sixty/tend/pull/774))

## 0.1.10

### Improved

- **The system prompt gains an explicit priority ordering.** A new `Priorities` section in the shared system prompt, loaded by every harness, ranks (1) being pro-social, (2) making the project excellent, and (3) helping individual users — so when an individual's workaround and the durable project-level fix pull apart, the bot foregrounds the durable fix. The triage skill adds a matching "apply the project lens" step before replying. ([#758](https://github.com/max-sixty/tend/pull/758))
- **The `tend-outage` failure reporter renders every entry as one table.** Follow-up failures on an open outage issue now append the same one-row `When | Run | Trigger` table the issue body uses, instead of a bespoke one-liner, so a single outage issue reads uniformly. ([#748](https://github.com/max-sixty/tend/pull/748))
- **Both Claude harnesses update to claude-code 2.1.201.** ([#755](https://github.com/max-sixty/tend/pull/755))

### Fixed

- **Bot commits and PRs are attributed solely to the tend bot.** Both Claude harness actions now set `attribution: {commit: "", pr: ""}` in the agent's settings, emptying Claude Code's auto-added `Co-Authored-By` commit trailer and "Generated with Claude Code" PR footer, and the triage, ci-fix, and review skill templates drop their hard-coded `Co-Authored-By` lines. ([#760](https://github.com/max-sixty/tend/pull/760))
- **`tend-mention` no longer runs a no-op session on the bot's own APPROVED review.** The bot's empty-body approval is terminal, but its `pull_request_review` event passed the engagement check (which counts that very review) and spun up a session with nothing to do. The verify job now skips when the review is the bot's own approval with an empty body — a bot review requesting changes, commenting, or approving with body text still fires. ([#749](https://github.com/max-sixty/tend/pull/749))

## 0.1.9

### Improved

- **PR review runs a `/code-review` pass alongside its manual checks.** The bundled `review` skill now pairs its manual read with a `/code-review` over the diff, with the effort tier scaled to how core the change is — peripheral or mechanical changes (docs, config, dependency bumps, test-only) get `low`/`medium`, core-logic changes get `high`/`max`. The "core" definition is left to each adopter's own guidance, so the bundled skill hardcodes no one project's components. ([#741](https://github.com/max-sixty/tend/pull/741))
- **The default harness timeout rises from 60 minutes to 3 hours.** Both Claude harness actions (`claude` headless and `claude-interactive` PTY) bump `timeout_seconds` from `3600` to `10800`, so genuinely long sessions finish instead of hitting the cap and surfacing as "Bot temporarily unavailable"; short tasks are unaffected. Codex has no harness-level timeout and is unchanged. ([#739](https://github.com/max-sixty/tend/pull/739))
- **The Recheck-Before-Posting guard catches a new directive, not just a duplicate.** The bundled `running-in-ci` skill now treats a comment that arrived mid-session as a possible maintainer follow-up — a correction or narrowed scope to fold into the same run — rather than only checking for redundant posts, and runs the re-fetch before ending the turn, not only before commenting. ([#738](https://github.com/max-sixty/tend/pull/738))
- **Both Claude harnesses update to claude-code 2.1.195.** ([#740](https://github.com/max-sixty/tend/pull/740))

### Fixed

- **Incremental PR review no longer leaks the base-merge delta.** The `review` skill's "what changed since my last review" pre-flight switched from a two-endpoint `A...B` compare to a three-dot diff with `git log --no-merges`, so a `Merge branch '<default>'` landing between review points no longer drags the entire merged-in base delta into the diff and makes the bot critique code the PR never authored. ([#735](https://github.com/max-sixty/tend/pull/735))

### Documentation

- **A composite-action comment drops a disproved security claim.** The surviving `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` comment no longer asserts that bubblewrap / the scrub knob "was hiding the model auth from the agent" — the evidence in [#637](https://github.com/max-sixty/tend/pull/637) showed Claude Code strips its own Anthropic credentials from bash subprocesses regardless of the knob. ([#737](https://github.com/max-sixty/tend/pull/737))

## 0.1.8

### Fixed

- **Bundled skills configure a git identity before committing in fresh worktrees.** The `nightly`, `running-in-ci`, and `review-runs` skills now set `git config --global user.name/email` (to the bot's noreply identity) before the bot's first commit. A `/tmp` worktree inherits no identity, so `git commit` previously failed with `Author identity unknown`, pushed an empty branch, and left `gh pr create` to error with `No commits between main and <branch>`. ([#730](https://github.com/max-sixty/tend/pull/730))

## 0.1.7

### Improved

- **Generated workflows pin `actions/checkout` to v7.** All generated workflows (and tend's own) move from checkout v6 to v7. The review workflow opts into v7's fork-PR checkout guard (`allow-unsafe-pr-checkout: true`), which otherwise refuses to check out a fork's `refs/pull/N/{merge,head}` under `pull_request_target` (the "pwn request" guard), so fork-PR reviews keep running. ([#725](https://github.com/max-sixty/tend/pull/725))
- **Both Claude harnesses update to claude-code 2.1.185.** ([#719](https://github.com/max-sixty/tend/pull/719))
- **The bot surfaces a blocking scope rule instead of silently routing around it.** When a `running-in-ci` scope restriction blocks the right action — e.g. engaging an existing upstream thread in another repo — the bot now surfaces the blocker on the triggering thread and offers either to take the upstream action on approval or to relax the rule via the consuming repo's `running-tend` overlay, rather than substituting a second-best local workaround without signaling it hit a wall. ([#717](https://github.com/max-sixty/tend/pull/717))

### Fixed

- **CI-poll loops fit the Bash tool's 10-minute cap.** The bundled `running-in-ci` poll recipes cap their `sleep` loops at 9 iterations and call the Bash step with `timeout: 600000`, so the harness no longer auto-backgrounds a longer loop and strands the gated follow-up (dismissing a stale approval, posting failure analysis). ([#695](https://github.com/max-sixty/tend/pull/695))
- **Nightly workflow-regen bases its worktree on an open PR, not branch-ref existence.** The `nightly` skill's regen step now bases on the `tend/update-workflows` branch only when an open PR rides it, and otherwise bases on `HEAD` and drops any leftover remote branch. A PR previously closed without merge no longer leaves a stale branch that inflates the diff, produces an inaccurate PR body, or defeats the no-value skip. ([#721](https://github.com/max-sixty/tend/pull/721))

### Documentation

- The codex `effort` value list in the README and the install-tend skill is corrected to `low | medium | high | xhigh`. ([#710](https://github.com/max-sixty/tend/pull/710))

### Internal

- Composite-action step bodies are de-duplicated into scripts under `shared/steps/`, and each harness action lives under a harness-named path. Generated workflows now invoke `max-sixty/tend/claude@X.Y.Z` (and `claude-interactive`) rather than the bare-root default; existing pinned refs keep resolving and the nightly regen stamps the new path automatically. ([#712](https://github.com/max-sixty/tend/pull/712))
- `review-reviewers` documents the `pull_request_review` self-trigger as expected (non-)behavior, and the `worker-deploy` comment corrects the live-stream count to two. ([#707](https://github.com/max-sixty/tend/pull/707), [#711](https://github.com/max-sixty/tend/pull/711))

## 0.1.6

### Improved

- **The default `claude` harness runs the official binary headless behind the credential proxy.** The root `action.yaml` was rewritten to run `claude -p` as a non-sudo `tend-sandbox` user behind the same credential-injecting mitmproxy the interactive harness uses, replacing the `anthropics/claude-code-action@v1` wrapper that handed the bot PAT and the Anthropic credential to the agent directly. Both credentials now live only in the proxy and never enter the agent's environment or disk; completion is the `claude -p` exit code. The action gains `claude_version`, `timeout_seconds`, and `mitmproxy_version` inputs and drops the unused claude-code-action passthroughs. ([#704](https://github.com/max-sixty/tend/pull/704))

### Internal

- Bundled skills replace guidance duplicated from `running-in-ci` — triage's recheck-before-posting and review-runs' read-only-mount workaround — with references to the canonical sections. ([#703](https://github.com/max-sixty/tend/pull/703))
- The `claude-smoke` workflow that exercises the headless harness end-to-end becomes `workflow_dispatch`-only, matching `interactive-smoke`. ([#706](https://github.com/max-sixty/tend/pull/706))

## 0.1.5

### Improved

- **Interactive harness isolates both credentials behind the proxy.** Phase 2 extends the credential-injecting proxy to the Anthropic model credential, so the sandboxed agent holds only dummies for both the GitHub PAT and the model token while the runner-owned proxy injects the real values per host. The agent toolchain now installs directly as the non-sudo sandbox user (dropping a ~200 MB per-run copy), and the proxy also injects the PAT for `raw.githubusercontent.com`. ([#686](https://github.com/max-sixty/tend/pull/686), [#683](https://github.com/max-sixty/tend/pull/683), [#684](https://github.com/max-sixty/tend/pull/684))
- **install-tend isolates each bot's auth in a per-bot `GH_CONFIG_DIR`.** Bot credentials live in a dedicated `~/.config/gh-bots/<bot-name>` dir selected per command and stored outside the OS keychain, removing the `gh auth switch` choreography that could strand a bot as the active account and 403 a maintainer's pushes. ([#688](https://github.com/max-sixty/tend/pull/688))
- **Interactive harness updated to claude-code 2.1.179.** The pinned `claude` binary resolves `--model opus` to Opus 4.8. ([#697](https://github.com/max-sixty/tend/pull/697))

### Internal

- Bundled skill refinements: nightly skips stamp-only workflow-regen PRs and scopes "Notable changes" to adopter-relevant entries, review-reviewers keeps an audit trail on empty-window cycles, and over-prescriptive guidance is reframed as examples and open frames. ([#693](https://github.com/max-sixty/tend/pull/693), [#692](https://github.com/max-sixty/tend/pull/692), [#689](https://github.com/max-sixty/tend/pull/689), [#690](https://github.com/max-sixty/tend/pull/690), [#675](https://github.com/max-sixty/tend/pull/675))
- tend-repo maintenance: a weekly task keeps the pinned agent binaries current, integration-fixture secrets reseed outside the sandbox, and the secret env-gating rejection analysis is recorded alongside a CLAUDE.md restructure. ([#696](https://github.com/max-sixty/tend/pull/696), [#685](https://github.com/max-sixty/tend/pull/685), [#687](https://github.com/max-sixty/tend/pull/687))

## 0.1.4

### Improved

- **Claude harnesses run with `bypassPermissions`.** The previous `dontAsk` mode hard-denies writes to Claude Code's protected paths (`.github/`, dotfiles), blocking autonomous fixes that touch those files. Everything the bot writes still lands through a reviewed PR. ([#677](https://github.com/max-sixty/tend/pull/677))
- **GitHub Releases publish on tag push.** The release workflow extracts the version's section from `CHANGELOG.md` and creates the release; 0.1.1–0.1.3 are backfilled. Nightly workflow-update PRs now summarize notable upstream changes instead of pasting a file list. ([#678](https://github.com/max-sixty/tend/pull/678))
- **install-tend triages an existing bot PAT before minting a new one.** The bot-token step runs the scope audit and routes to reuse, scope refresh, or first-time login. ([#680](https://github.com/max-sixty/tend/pull/680))
- The review skill checks the PR's check rollup before approving, so visible CI failures aren't rubber-stamped. ([#667](https://github.com/max-sixty/tend/pull/667))

### Fixed

- The interactive harness passes GitHub Actions context env vars (`GITHUB_RUN_ID`, `GITHUB_REPOSITORY`, …) into the sandbox; skill recipes for run self-reference and URL construction depend on them. ([#664](https://github.com/max-sixty/tend/pull/664))

### Documentation

- README clarifies that the weekly workflow approves dependency PRs rather than auto-merging them. ([#673](https://github.com/max-sixty/tend/pull/673))

### Internal

- Skill refinements across running-in-ci, triage, and ci-fix: end the turn only when work is shipped, defer test suites to PR CI, split CI monitoring into gated/ungated cases, label transient-tracker issues `tend-outage`, and carve out bot-authored machine-report issues. ([#661](https://github.com/max-sixty/tend/pull/661), [#671](https://github.com/max-sixty/tend/pull/671), [#669](https://github.com/max-sixty/tend/pull/669), [#670](https://github.com/max-sixty/tend/pull/670), [#666](https://github.com/max-sixty/tend/pull/666))

## 0.1.3

### Improved

- **Interactive harness isolates the GitHub PAT.** The agent runs as a non-sudo `tend-sandbox` user; the PAT lives only in a local credential-injecting proxy that adds it for GitHub hosts, so it never enters the agent's environment. ([#652](https://github.com/max-sixty/tend/pull/652))
- **Prior-run context recall.** The bot recalls context from earlier runs on the same issue or PR instead of starting cold each invocation. ([#649](https://github.com/max-sixty/tend/pull/649))
- tend now dogfoods the `claude-interactive` harness for its own review/mention/triage/ci-fix workflows. ([#622](https://github.com/max-sixty/tend/pull/622))

### Fixed

- **Worker reliability:** throw on Search failures instead of caching an empty payload, return 503 (not a 200 all-zero payload) on cold-cache failure, and share the `/activity` payload across colos via a KV tier. ([#648](https://github.com/max-sixty/tend/pull/648), [#650](https://github.com/max-sixty/tend/pull/650), [#653](https://github.com/max-sixty/tend/pull/653))
- Site `liveData` polling self-schedules so ticks can't overlap. ([#655](https://github.com/max-sixty/tend/pull/655))
- Mention workflow skips no-op sessions for undirected bot comments. ([#608](https://github.com/max-sixty/tend/pull/608))
- `review-reviewers` pre-creates the monthly tracking issue to eliminate a matrix race. ([#657](https://github.com/max-sixty/tend/pull/657))

### Internal

- Skill refinements across running-in-ci, notifications, review-runs, ci-fix, and running-tend; dead-input and template cleanups in the actions and generator.

## 0.1.2

### Improved

- **`claude-interactive` harness.** A PTY-supervised alternative to the Agent SDK: runs the official `claude` binary under a `script(1)` supervisor with a Stop-hook sentinel. ([#609](https://github.com/max-sixty/tend/pull/609))
- **Per-workflow harness override.** Trial a different harness (and matching model) on one workflow at a time. ([#612](https://github.com/max-sixty/tend/pull/612))

### Fixed

- Mention workflow uses `comment.updated_at` so edit events report accurate queue delay. ([#595](https://github.com/max-sixty/tend/pull/595))
- Interactive harness: token-usage jq parser double-iterator fix, and the `-newer` filter dropped from the session-JSONL parser. ([#616](https://github.com/max-sixty/tend/pull/616), [#614](https://github.com/max-sixty/tend/pull/614))

### Internal

- Skill hardening: close the env-filter loophole for `ALL_INPUTS` secrets, recheck PR state before pushing follow-up commits, raise the bar for repo-overlay PRs, and trigger upstream-bot rebases instead of manual conflict resolution. ([#599](https://github.com/max-sixty/tend/pull/599), [#573](https://github.com/max-sixty/tend/pull/573), [#604](https://github.com/max-sixty/tend/pull/604), [#605](https://github.com/max-sixty/tend/pull/605))

## 0.1.1

### Internal

- Skill refinements: a weekly integration-test recipe and a release-workflow fix. ([#590](https://github.com/max-sixty/tend/pull/590), [#589](https://github.com/max-sixty/tend/pull/589))
