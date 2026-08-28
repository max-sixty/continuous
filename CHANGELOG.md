# Changelog

Notable changes per release. The `## X.Y.Z` section for each version is
published verbatim as that version's GitHub Release notes
(`.github/workflows/pypi-release.yaml`). Newest first. Releases before
0.1.1 predate this changelog; see the compare views at
https://github.com/max-sixty/tend/compare for their history.

## 0.1.20

### Fixed

- **Claude sandbox setup keeps tools installed by adopter setup actions.** The action now carries the runner's original `PATH` across its fixed-path privilege boundary as data, so tools added through `GITHUB_PATH` remain available to `sandbox_setup:` and the agent. The hosted sandbox test executes the action's production launcher, closing the test gap that allowed 0.1.19 to discard those paths. ([#1071](https://github.com/max-sixty/tend/pull/1071))

## 0.1.19

### Improved

- **`sandbox_setup:` receives the agent's GitHub context and uses an explicit home boundary.** Safe `GITHUB_*` values now cross through one shared launch-environment helper, after adopter `sandbox_env:` values so the run context wins. Claude's sandbox inherits shared system and hosted-toolcache paths plus independently seeded sandbox-home tools; later home-scoped installs belong in `sandbox_setup:`. ([#1041](https://github.com/max-sixty/tend/pull/1041), [#1047](https://github.com/max-sixty/tend/pull/1047), [#1048](https://github.com/max-sixty/tend/pull/1048), [#1057](https://github.com/max-sixty/tend/pull/1057), [#1059](https://github.com/max-sixty/tend/pull/1059))
- **An all-defaults install starts with one three-question dialog.** A connected Chrome session drives signup and registry steps until it reaches a password, CAPTCHA, or 2FA prompt; OAuth and API-key fallbacks keep credentials out of chat. The flow resumes incomplete installs, repairs overlay frontmatter, and runs `tend check` after its prerequisites exist. ([#1009](https://github.com/max-sixty/tend/pull/1009), [#1010](https://github.com/max-sixty/tend/pull/1010), [#1011](https://github.com/max-sixty/tend/pull/1011), [#1012](https://github.com/max-sixty/tend/pull/1012), [#1017](https://github.com/max-sixty/tend/pull/1017), [#1018](https://github.com/max-sixty/tend/pull/1018))
- **Review depth follows the behavior a change asserts.** Documentation about code behavior gets a core review; a review of the bot's own PR stops at the review so the successor session applies it. Compute-only changes require observed recurrence and one local knob or removal, judged across the whole change. ([e909680](https://github.com/max-sixty/tend/commit/e90968095c68459735e3446ee44d911d64bfc38d), [#1007](https://github.com/max-sixty/tend/pull/1007), [acd9d32](https://github.com/max-sixty/tend/commit/acd9d32bbcb46b23c42817dc0d4922c3044142b5), [#1024](https://github.com/max-sixty/tend/pull/1024))
- **Bots carry maintainer decisions forward.** Before proposing a fix they search closed PRs and issue comments for rejected attempts, and reusable corrections route to tend's bundled skills instead of a consumer overlay. PR, issue, and commit titles use literal descriptions that stand on their own. ([#1014](https://github.com/max-sixty/tend/pull/1014), [#1022](https://github.com/max-sixty/tend/pull/1022), [#1060](https://github.com/max-sixty/tend/pull/1060), [#1062](https://github.com/max-sixty/tend/pull/1062))

### Fixed

- **Fork PR instruction files are pinned at every depth.** Both harnesses restore `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, and `.claude/**` from the base tree, without following fork-created symlinks or changing the index. The Claude sandbox also globally ignores its generated `.claude/settings.local.json` so broad staging does not sweep `bypassPermissions` into a PR. ([#1005](https://github.com/max-sixty/tend/pull/1005), [#1029](https://github.com/max-sixty/tend/pull/1029), [#1034](https://github.com/max-sixty/tend/pull/1034))
- **CI polling handles large check rollups and rejects unusable commit IDs immediately.** `poll-pr-checks.sh` paginates past 100 contexts, preserves fail-closed behavior if a page is unusable, and rejects abbreviated, uppercase, or missing OIDs before sleeping or falsely reporting that the branch advanced. ([#999](https://github.com/max-sixty/tend/pull/999), [#1046](https://github.com/max-sixty/tend/pull/1046))
- **Scheduled recovery keeps the evidence it needs.** `review-runs` now collects review bodies as well as thread comments and dispositions in a tested script; `nightly` leaves outage trackers for that workflow to drain, and rechecks conflict resolution against freshly fetched `main` before polling CI. `ci-fix` scopes outage escalation by the tracker title as well as its label. ([#1015](https://github.com/max-sixty/tend/pull/1015), [#1021](https://github.com/max-sixty/tend/pull/1021), [#1027](https://github.com/max-sixty/tend/pull/1027), [#1032](https://github.com/max-sixty/tend/pull/1032))
- **Workflow generation writes valid UTF-8 on non-UTF-8 locales.** All generator text I/O and the OAuth helper now specify UTF-8 explicitly, avoiding cp1252-corrupted workflow YAML on Windows. ([#1058](https://github.com/max-sixty/tend/pull/1058))
- **The activity feed now uses four GitHub Search requests per refresh.** Each bucket folds every bot into one query instead of making one query per bot, while preserving counts and recent links. ([#1040](https://github.com/max-sixty/tend/pull/1040))

### Documentation

- **Security guidance matches both harnesses' credential surfaces.** It points adopter-specific policy at the pinned overlay, describes the Claude credential proxy and Codex's live credentials, and forbids process or environment output that can expose either harness's values. ([#997](https://github.com/max-sixty/tend/pull/997), [b850ced](https://github.com/max-sixty/tend/commit/b850ced2625d9db2007c95bd1e8133b784cf8f21), [#1044](https://github.com/max-sixty/tend/pull/1044), [#1056](https://github.com/max-sixty/tend/pull/1056))

### Internal

- Claude Code moves to 2.1.241, Codex to 0.149.0, uv to 0.12.5, and `astral-sh/setup-uv` to v10.0.1; repo-local hooks and action pins are refreshed with them. ([#1000](https://github.com/max-sixty/tend/pull/1000), [#1001](https://github.com/max-sixty/tend/pull/1001), [#1002](https://github.com/max-sixty/tend/pull/1002), [#1003](https://github.com/max-sixty/tend/pull/1003), [#1037](https://github.com/max-sixty/tend/pull/1037), [#1038](https://github.com/max-sixty/tend/pull/1038), [#1039](https://github.com/max-sixty/tend/pull/1039))
- Proxy tests assert that the `--allow-hosts` argument and credential-injection host sets stay in sync. ([#1049](https://github.com/max-sixty/tend/pull/1049))

## 0.1.18

### Improved

- **A PR description's claims are scoped to the merge base.** A review run reads its diff from the last commit it reviewed, but the description it edits describes the whole PR, so an increment's conclusion went in as if it covered everything. The two bases sit far apart on a long-lived branch, where nightly's rolling `tend/update-workflows` PR accumulates a release per run, and a wrong description never turns a check red. `running-in-ci` carries the scope rule where the description edit happens; a session that can only verify the increment names that base instead of writing an unscoped claim. ([#992](https://github.com/max-sixty/tend/pull/992))
- **A session on a fork PR actions its own review's findings.** `tend-mention`'s relay is gated on the PR's head repo, so a review on a fork PR dispatches no successor session, and the notifications poll can't see it either, since GitHub doesn't notify an actor of their own activity. A session that pushed the commits under a maintainer directive now actions its own review before ending; otherwise it names the findings as unowned in its closing comment. `running-in-ci` also records why `reviews` has to stay in the `--json` projection the pre-post recheck reads. ([#985](https://github.com/max-sixty/tend/pull/985))

### Fixed

- **`repo-secret-allowlist` reports only the org secrets the repo can read.** It flagged every org-level secret as available to all workflows without consulting visibility, so a repo where all of them are `visibility: selected` and none list it got a FAIL nothing could clear. That repo is already at the tightest scoping GitHub offers, and the one remaining lever, a `secrets.allowed` entry, would mute those names permanently. `_list_org_secrets` now drops what this repo can't reach: `selected` whose repositories list omits it (paginated), `private` against a public repo, and all of them against a private repo in a GitHub Free org. A secret whose reach can't be determined stays in the reported set, since under-reporting hides real exposure, and `check_secrets` reads the same helper. ([#994](https://github.com/max-sixty/tend/pull/994))
- **Both halves of the 👀 reaction sit in one job.** `tend-mention` put the eyes on in `verify` and took them off in `handle`, but `always()` only governs steps in a job that started, and GitHub evicts a *pending* job when a newer run arrives. So a burst of mentions on one thread left a 👀 claiming a session was working, with no session behind it and nothing left to remove it. `react_eyes` moves to `handle`'s first step, matching `review` and `triage`. The removal step's lookup also paginates now, because 👀 is the ordinary "watching this" reaction too: on a busy issue the bot's own fell past the first page, and the step exited 0 having kept it. ([#990](https://github.com/max-sixty/tend/pull/990))

### Internal

- The `uv_build` range moves to the 0.12 line, matching the uv that runs the build. The old `<0.12` bound left every `uv build` and workspace `uv sync` printing a version warning; `test_repo_pins.py` now asserts the range contains the `uv_version` pinned in `claude/action.yaml`, so the weekly sweep that bumps that pin carries the build backend along. ([#988](https://github.com/max-sixty/tend/pull/988))
- The Codex Cloud setup script calls `wt config approvals add --yes` instead of rendering `approvals.toml` by hand, at Worktrunk 0.74.0, where that command no longer refuses a non-TTY. ([#986](https://github.com/max-sixty/tend/pull/986), [#987](https://github.com/max-sixty/tend/pull/987))
- `publish-site.yaml` runs its build job on any PR touching `site/` or the workflow itself, so an astro bump can't merge green and then break on the way to Pages. ([#986](https://github.com/max-sixty/tend/pull/986))

## 0.1.17

### Improved

- **👀 marks a session in flight and comes off when it ends.** The bot reacts on the issue or PR as a session picks it up — triage on an opened issue, review on every run that clears its gate — and on a comment that mentions the bot, as before. The reaction is released at the end of the run under `always()`, so a failed or cancelled session doesn't strand it. Mention's comment target is computed from `github.event_name` rather than probed across both endpoints, so an unrelated issue comment that happens to share a review comment's id can no longer take the reaction. ([#974](https://github.com/max-sixty/tend/pull/974), [#979](https://github.com/max-sixty/tend/pull/979))

### Fixed

- **`credential-environments` verifies the tag ruleset from the bot's own token.** GitHub serves a ruleset's `bypass_actors` only to repo admins, so a run as the bot could never confirm the all-tags ruleset tend prescribes: the check parked at `SKIP` on every repo whose credential-holding environments admit tags, and the nightly's drift issue stayed open indefinitely. Where the caller is the bot, the check now reads `current_user_can_bypass` — GitHub's own evaluation of the caller against the full bypass list, principals the actor-list inference can't resolve included — and credits `"never"` as gated. Any other caller's verdict says nothing about the bot and falls through to the existing inference. ([#976](https://github.com/max-sixty/tend/pull/976))
- **The Codex harness pins `.claude/` to the base branch on a fork PR.** `codex/agents-tail.md` tells the agent that repo-local skills live at `.claude/skills/<name>/SKILL.md` and to read them in full, while the fork-PR defense restored only `CLAUDE.md` and `AGENTS.md` — so a fork-authored `running-tend` skill loaded as trusted repo guidance. `shared/steps/pin-instruction-files.sh` restores the whole of `.claude/` alongside those files, through git and without following destination symlinks, so a fork symlink can't redirect a write outside the checkout. The Claude harness was unaffected: `restore-sensitive-config.sh` already covered `.claude`. ([#978](https://github.com/max-sixty/tend/pull/978), [#980](https://github.com/max-sixty/tend/pull/980))

### Internal

- One `uv run pytest` from the repo root runs every Python suite. The root is a uv workspace with `generator/` as its only member, so `generator/`, `proxy/`, and the install-tend scripts share one environment — the pinned mitmproxy the proxy addon imports included. `dev/test.sh` is deleted, `wt test` is four named steps in `.config/wt.toml`, and CI's three Python jobs become one; `test_pinned_mitmproxy_matches_the_action` asserts the installed distribution matches `claude/action.yaml`'s default. ([#977](https://github.com/max-sixty/tend/pull/977))
- A Codex Cloud setup script installs pinned Worktrunk, verifies the `.config/wt.toml` command approvals, syncs the generator, site, and worker dependencies, and installs `pre-commit` without git hooks. `.config/codex-cloud/README.md` documents the Cloud environment settings. ([#978](https://github.com/max-sixty/tend/pull/978))
- `nightly` records why the `commits(first: 100)` cap can't truncate its human-commit guard: the guard runs only on `dependabot[bot]` / `renovate[bot]` PRs, whose branches carry one or two commits. ([#975](https://github.com/max-sixty/tend/pull/975))

## 0.1.16

### Improved

- **A finding clears a cost gate before the bot acts on it.** Gate 3 classifies each failure by what the observed occurrence left on the public record. A wrong outward action (something posted, approved, merged, or closed in error) justifies real mechanism; wasted compute (a no-op session, a duplicated survey, a run a later tick retries) supports only a fix that is itself nearly free: a cadence value, a deleted step, a one-line condition. That holds at any occurrence count, and a hypothetical chain from waste to a wrong action doesn't upgrade the class; aggregated waste escalates to the maintainer as a number instead. `running-in-ci` carries the value order behind the gate and a five-open-PR budget on self-initiated improvements, which never holds requested work or product fixes. ([#960](https://github.com/max-sixty/tend/pull/960))
- **The CI-poll recipes are tested scripts, and they poll the commit the session is accountable for.** `poll-pr-checks.sh` reads the status-check rollup by commit OID rather than through the PR, so a push by another actor mid-poll can't return a green verdict for code this session never pushed. It buckets the terminal-and-red conclusions that previously fell through as green (`STARTUP_FAILURE`, `ACTION_REQUIRED`), reports UNVERIFIED rather than green for a null rollup or a context list past the query's one 100-node page, and reads a check group with any non-terminal entry as pending. `rerun-failed-jobs.sh` finds the new attempt's jobs by `run_attempt`, so a fast rerun no longer reads as "nothing re-queued" and an unregistered one can't report stale conclusions as fresh. ([#965](https://github.com/max-sixty/tend/pull/965))
- **`list-recent-runs.sh` anchors its window on the last successful run.** The floor is that run's start time, clamped 6h back, and when the clamp bites or no successful run exists a warning on stderr tells the caller to record a coverage gap rather than an all-clear. Consecutive windows overlap by construction at any cadence, which retires the cron parsing, tick tiling, dropped-tick recovery, and the retry wrapper: a transient failure now fails loudly, and the next tick's floor reaches back past the lost window. ([#965](https://github.com/max-sixty/tend/pull/965))
- **One resolver answers which review anchors the current head.** A reply to a review thread makes GitHub wrap the reply in a synthetic zero-body review at the then-current head, and a force-push re-points earlier reviews' `commit_id` at the new one, so every caller has to discount both. Five did, in untested shell that had drifted far enough for `weekly` to document its filter order as deliberately different from `review`'s. `bot-review-state.sh` emits the answer as JSON (the newest substantive review, whether a rewrite postdates it, what anchors the current head, the orphan body a partially-failed review POST left behind, and the fresh and stale approvals), and each call site reads one field. ([#971](https://github.com/max-sixty/tend/pull/971))
- **`tend-mention`'s verify gate keeps two structural rules where it had four.** A bot-authored review summons a session only as the reviewer-to-author handoff (fresh content on a PR the bot authored), and a contentless approval is terminal whoever submitted it. GitHub rejects self-approvals, so the empty-body `APPROVED` gate was already covered by the author-keyed rule. Outward behavior is unchanged and each rendered workflow drops about 52 lines. ([#965](https://github.com/max-sixty/tend/pull/965))
- **`review-reviewers` runs on `workflow_dispatch` alone.** Its recent output had converged on maintaining its own evidence machinery at the fleet's highest per-run cost, and its seven-day quota ceiling twice took the user-facing workflows down with it; each adopter's daily `tend-review-runs` reviews that repo's runs from the inside and routes bundled-skill defects upstream. The skill describes the window `list-recent-runs.sh` hands it rather than calling itself hourly, and scopes its claims to that window. Dispatch it after a change that could move bot behavior fleet-wide: a release, a harness switch, a model bump. ([#966](https://github.com/max-sixty/tend/pull/966))

### Fixed

- **Head-keyed guards work on a PR past 100 commits.** `gh pr view --json commits` issues an unpaginated, oldest-first `commits(first: 100)`, so `.commits[-1]` was commit #100 rather than the head. Every head-keyed field then matched nothing and fell silent rather than failing loudly: the pre-post guard stopped firing, so a fold-in re-run posted a second review on a commit it had already reviewed; the 422 recovery found no orphan and duplicated instead of editing; `review`'s moved-head check inverted, so a push that did move the head read as "HEAD hasn't moved" and the review posted against a stale commit; and `weekly`'s redundant-approval guard collected an approval per weekly run on one commit. Four sites read `headRefOid`, which is the head by definition and can't truncate. ([#971](https://github.com/max-sixty/tend/pull/971))
- **A rebased dependency PR no longer carries an approval nothing earned.** `weekly` skipped re-approving when the last approval's commit matched the head, but a force-push re-anchors the prior review at the new head, so that comparison read true for a commit the bot never saw. Dependency PRs are the population `nightly` rewrites on purpose, through `@dependabot recreate` and renovate's `rebase-check`. Approvals older than the newest `head_ref_force_pushed` are now dropped, and the two paths that skip without approving (CI failing, major version bump) dismiss a stale approval rather than leaving a rebased-into-red PR reading as bot-approved. ([#890](https://github.com/max-sixty/tend/pull/890))
- **`review-runs` appends to the live evidence log.** jq's `^` anchors to the start of the string, so `test("^## Run [0-9]")` matched no log comment whose body opens with a blank line or a `---` separator, which is the ordinary shape its own rollover branch posts. The selector then fell back to an older comment, in practice the one rolled over for nearing GitHub's 65 KB body limit, and from there a run appended out of chronological order or took the rollover branch again every run, both silently. The selector anchors with `(^|\n)`. ([#963](https://github.com/max-sixty/tend/pull/963))

### Documentation

- `claude_version` tracks npm's `latest` dist-tag, with the reason `stable` isn't the safer-looking pin recorded in `CLAUDE.md`: `install.sh` downloads the `latest` build as its bootstrap installer whatever the target, so only the agent session runs the pin. ([#959](https://github.com/max-sixty/tend/pull/959))
- The worker README's `/activity` description names all five bookkeeping labels the filter drops; `tend-rate-limit` was missing. ([#969](https://github.com/max-sixty/tend/pull/969))

### Internal

- The mention engagement gate moves from `mention.yaml.j2` into `generator/src/tend/templates/mention-verify.sh`, the shape `review-gate.sh` and `notifications-check.sh` already use, unchanged line for line. 38 tests run the script against a fake `gh` whose `--jq` goes through real jq, where the rendered YAML could only be checked for substrings in a certain order. Generation keeps the `env:` block pinned by exact value, with a scan proving nothing the script reads is left unset: an unwired name is empty at runtime, so the gate would answer on a blank while the job stayed green. ([#970](https://github.com/max-sixty/tend/pull/970))
- `running-in-ci` moves grounded-analysis depth, the other-repos rules, and the bar for proposing a skill PR into `references/`, which load on demand. ([#965](https://github.com/max-sixty/tend/pull/965), [#971](https://github.com/max-sixty/tend/pull/971))
- pre-commit's shellcheck covers `plugins/**/*.sh`, which it previously skipped entirely, and the shell-script suites share one fake-`gh` scaffold in `generator/tests/__init__.py`. ([#965](https://github.com/max-sixty/tend/pull/965))
- CI's `test-worker` job runs on Node 22, matching the deploy job that re-runs it, and `worker/package.json` declares `"engines": {"node": ">=22.0.0"}`, the floor Wrangler enforces itself. ([#961](https://github.com/max-sixty/tend/pull/961))

## 0.1.15

### Improved

- **A maintainer can release the spike rate limit without waiting for the UTC rollover.** A trip files or reopens a `tend-rate-limit` issue listing the runs it refused: opening it is the notice, closing it is the approval, and each close doubles the day's ceiling, so the breaker re-arms after use. An approval counts only from a non-bot, non-App actor closing the preflight's own pause issue after the label went on, so the bot can't arrange one for itself; the burst limit stays non-resumable and files no issue at all. The bot's identity for both the counts and the approval filter now comes from the credential (`gh api user`) rather than config. ([#874](https://github.com/max-sixty/tend/pull/874), [e5f0f9b](https://github.com/max-sixty/tend/commit/e5f0f9b5))
- **`tend-review` folds a mid-review push into the running session instead of cancelling it.** The workflow moves to `cancel-in-progress: false`, the session stamps each HEAD it examines as a `tend-review/<pr>` commit status, and a pre-check skips a queued run whose live HEAD already carries the stamp. A push mid-review no longer discards what the session had read. ([#903](https://github.com/max-sixty/tend/pull/903))
- **`tend check`'s `credential-environments` reads repo shapes it previously misread.** Environment names are read one per line and addressed URL-encoded, so a name containing a space or a `/` no longer 404s and skips the whole check ([#879](https://github.com/max-sixty/tend/pull/879)); a workflow declaring `workflow_call` beside its own triggers is reachable rather than external-only ([#932](https://github.com/max-sixty/tend/pull/932)); a call out of the repo is followed, and a workflow only an outside caller can start is no longer treated as this repo's surface ([#940](https://github.com/max-sixty/tend/pull/940)); a ruleset bypass list the token can't read reports unknown instead of ungated ([#825](https://github.com/max-sixty/tend/pull/825)); and the failure's remedy no longer names branch protection, the one setting the gate rejects ([#900](https://github.com/max-sixty/tend/pull/900)).
- **`install-tend` loads for a repo that already has tend installed.** Its description read as install-time only, so a session clearing a failing `tend check` — the case it covers best — skipped it and reconstructed the procedure by hand. ([#928](https://github.com/max-sixty/tend/pull/928))
- **The four `secrets.*` name overrides are removed.** The operational secret names are module constants the templates and `tend check` share, so the names a workflow reads can't drift from the names the check verifies; a config still carrying an override fails with the rename to make. `install-tend` creates the `tend` environment and fills it from scratch, so there is no pre-existing secret whose name is worth keeping. ([#861](https://github.com/max-sixty/tend/pull/861))

### Fixed

- **`tend init --dry-run` no longer runs the legacy TOML→YAML migration.** The migration ran before the flag was consulted, so the preview wrote `.config/tend.yaml` and deleted `.config/tend.toml` — unrecoverable if the TOML was uncommitted. `--dry-run` now renders the migrated config in memory and reports what it would do, and a config that wouldn't round-trip still fails loudly. ([#942](https://github.com/max-sixty/tend/pull/942))
- **A generated review prompt no longer doubles its braces.** `generate_review` escaped them for `format()` on the path that never calls it. ([#935](https://github.com/max-sixty/tend/pull/935))
- **`tend-review` and `tend-mention` restore the adopter's local `setup:` composite before POST cleanup.** Both load the composite from the workspace and then land the PR's tree over it, so at job end the runner re-read the PR's version of the action file and dispatched the wrong POST steps. ([#847](https://github.com/max-sixty/tend/pull/847))
- **The `review` skill submits before it pushes.** A fix pushed ahead of the review cancelled the run that was about to post it, and the author side batches its pushes for the same reason ([#834](https://github.com/max-sixty/tend/pull/834), [#868](https://github.com/max-sixty/tend/pull/868)). The pre-APPROVE rollup dedupes checks by name and decides cap expiry on provenance, so a superseded cancellation artifact no longer sits alongside its replacement and holds up approval ([#821](https://github.com/max-sixty/tend/pull/821)). The mandated `/tend-ci-runner:code-review` second pass is unconditional after four of four sessions reached step 4 and skipped it ([#937](https://github.com/max-sixty/tend/pull/937)), and a withheld approval no longer cites a repo review policy that isn't written down ([#930](https://github.com/max-sixty/tend/pull/930)).
- **`tend-review` re-reviews after a force-push.** GitHub re-points a prior review's `commit_id` at the new head, so the pre-flight guard read `LAST_REVIEW_SHA == HEAD_SHA`, exited, and left a stale APPROVE standing as the verdict on code nothing had read. ([#884](https://github.com/max-sixty/tend/pull/884))
- **`tend-mention` no longer starts a session that can only no-op.** Three triggers are gated: the synthetic zero-body review container the bot's own inline reply creates ([#849](https://github.com/max-sixty/tend/pull/849)), a third party's empty-body `APPROVED` with no inline comments ([#955](https://github.com/max-sixty/tend/pull/955)), and the bot's own review on a PR someone else authored ([#916](https://github.com/max-sixty/tend/pull/916)) — in each case the run had no role to act in, and each was a billable session.
- **The Claude action accounts a cancelled session from its session JSONL** instead of writing an all-zeros `token-usage.json` for a run that may have posted a review, and it names the cause in the exited-non-zero outage annotation rather than exiting before the verdict is read. ([#873](https://github.com/max-sixty/tend/pull/873), [#818](https://github.com/max-sixty/tend/pull/818))
- **A run that fails ahead of the agent step reports an outage.** `Report failure` was gated on the agent step, so a failure in checkout, setup, or preflight left no tracker row at all. ([#857](https://github.com/max-sixty/tend/pull/857))
- **Outage trackers reconcile to one issue and append without losing rows.** A duplicate created in a race is reconciled by probing the numbers around the one just filed rather than re-reading the list that lost the race ([#836](https://github.com/max-sixty/tend/pull/836)); a failed `gh issue list` is told apart from "no issue is open", so a transient read no longer files a second tracker ([#901](https://github.com/max-sixty/tend/pull/901)); a matrix workflow posts one comment per run instead of one per leg ([#809](https://github.com/max-sixty/tend/pull/809)); and a failed row append warns instead of aborting the step ([#947](https://github.com/max-sixty/tend/pull/947)).
- **`mark-notification-read` treats a `null` `run_started_at` as absent.** `gh --jq` prints the literal string `null` for a 200 whose body lacks the field, which passed the `-z` guard added for the failure case. ([#862](https://github.com/max-sixty/tend/pull/862))
- **The `nightly` skill test-merges bot PRs with `git merge-tree`** rather than filtering on GitHub's lazily-computed `mergeable` field. A cold read returns `UNKNOWN`, which the filter dropped, so the step read "I don't know" as "no conflicts" and skipped the rebase pass entirely. ([#898](https://github.com/max-sixty/tend/pull/898))
- **A session in `running-in-ci` re-reads state immediately before an action that can't be taken back.** The branch is re-read before a PR is closed or merged, after one session closed a PR another had just pushed the review's remedy to ([#944](https://github.com/max-sixty/tend/pull/944)); a comment's dedup re-fetch happens immediately before the post rather than earlier in the step, and a sweep counts as a sibling ([#893](https://github.com/max-sixty/tend/pull/893)); a recipe is read before it's run and never extracted by position, after a session closed a live issue by running the wrong block ([#899](https://github.com/max-sixty/tend/pull/899)); a review's inline comments are fetched as part of reading it, after a bot published the exact thing a maintainer's inline comment had ruled out ([#950](https://github.com/max-sixty/tend/pull/950)); and a review that lands mid-poll is left to `tend-mention` instead of being fixed by both actors at once ([#870](https://github.com/max-sixty/tend/pull/870)).
- **`running-in-ci`'s poll caps are terminal in both loops.** The CI monitor and the `gh run rerun --failed` loop each exit non-zero on exhaustion and print every job's conclusion, so an agent reading the transcript can tell "all jobs finished" from "the budget ran out". ([#876](https://github.com/max-sixty/tend/pull/876), [#951](https://github.com/max-sixty/tend/pull/951))
- **A GitHub-status probe that doesn't answer with JSON no longer reads as no open incident.** The recipe printed nothing either way, so an upstream outage looked clear and the run filed the workaround PR the check exists to prevent. ([#913](https://github.com/max-sixty/tend/pull/913))
- **A fully scoped fix opens a PR rather than an issue `tend-triage` converts into one**, and an overlay proposal states the rule rather than the incident behind it. ([#914](https://github.com/max-sixty/tend/pull/914), [#842](https://github.com/max-sixty/tend/pull/842))
- **Bundled skills pass `--limit` on the dedup and survey `gh list` calls.** The default 30 capped exactly the scans that decide whether an artifact already exists, with no signal that they had truncated. ([#931](https://github.com/max-sixty/tend/pull/931))
- **The `--body` exemption is keyed on shell hazards rather than line count.** A one-line comment containing markdown inline code was passed as a double-quoted shell argument and bash ran the backticks, deleting the comment's leading words. ([#882](https://github.com/max-sixty/tend/pull/882))
- **`token-report` raises the per-workflow run limit and says when it's hit.** `gh run list --limit 100` stops silently, so a busier workflow's oldest runs were dropped and totalled at zero. ([#887](https://github.com/max-sixty/tend/pull/887))
- **`install-tend`'s OAuth wrapper answers the paste prompt.** `claude setup-token` can finish on the hosted callback page, where the TUI waits for a pasted `code#state`; the wrapper ran the child with stdin from `/dev/null`, so that path hung until killed with nothing in its output to say why. ([#881](https://github.com/max-sixty/tend/pull/881))
- **The `review-runs` and `review-reviewers` audit passes survey the window they claim to.** The run census pages instead of stopping at the first 30 runs, which had reported an hour as a day ([#886](https://github.com/max-sixty/tend/pull/886)), and covers every workflow prefix priced downstream, not `tend-*` alone ([#889](https://github.com/max-sixty/tend/pull/889)). A window a survey reports empty is swept repo-wide before that's believed ([#885](https://github.com/max-sixty/tend/pull/885)), Step 4 looks for in-thread maintainer corrections rather than only PR dispositions ([#833](https://github.com/max-sixty/tend/pull/833)), and a trigger stranded by a failed session is drained ([#851](https://github.com/max-sixty/tend/pull/851), [#949](https://github.com/max-sixty/tend/pull/949)). The window itself is anchored on the predecessor run's start rather than "24 hours before now", which had opened it after the predecessor closed and dropped every run in the gap ([#939](https://github.com/max-sixty/tend/pull/939)), and dropped-tick recovery anchors on the last *successful* run, so a run that died before producing analysis no longer advances the floor past an hour nothing examined ([#838](https://github.com/max-sixty/tend/pull/838)).
- **Their shared evidence log is found, sized, and attributed by what it's about.** It is located by its heading rather than by "newest bot comment" ([#875](https://github.com/max-sixty/tend/pull/875)); an append is sized against the combined body rather than the existing comment, against the 65 KB limit ([#945](https://github.com/max-sixty/tend/pull/945)); output counts as accepted only when a named non-bot actor accepted it ([#864](https://github.com/max-sixty/tend/pull/864)); a run is credited for an output from its own log rather than from wall-clock inclusion ([#869](https://github.com/max-sixty/tend/pull/869)); a relayed `repository_dispatch` run is mapped from its verify-job payload ([#941](https://github.com/max-sixty/tend/pull/941)); and people are named without `@`, so a log doesn't ping and subscribe them ([#892](https://github.com/max-sixty/tend/pull/892)).
- **A `review-reviewers` matrix leg no longer takes the others down with it.** Each leg gets its own PR branch name instead of sharing one derived from the run id ([#858](https://github.com/max-sixty/tend/pull/858)), a skipped init-tracking step doesn't cancel the matrix ([#877](https://github.com/max-sixty/tend/pull/877)), and dedup covers merged PRs and checks tend before filing upstream ([#891](https://github.com/max-sixty/tend/pull/891)).

### Documentation

- Scheduled-workflow intervals are described as a requested cadence: GitHub runs `schedule` triggers best-effort and drops ticks under load. ([#934](https://github.com/max-sixty/tend/pull/934))
- Every reserved `sandbox_env` key is named, with a test that fails when the list and the code diverge. ([#936](https://github.com/max-sixty/tend/pull/936))
- The marketplace description names Codex ([#863](https://github.com/max-sixty/tend/pull/863)); the release and consumers-search notes point at the harness-scoped action ref ([#894](https://github.com/max-sixty/tend/pull/894)); `site/README.md` no longer tells readers to start a dev server `wt` already started ([#952](https://github.com/max-sixty/tend/pull/952)).

### Internal

- `wt test` runs all four suites CI runs rather than `generator/` alone ([#896](https://github.com/max-sixty/tend/pull/896)), ruff covers every Python file ([#895](https://github.com/max-sixty/tend/pull/895)), the shell-script tests resolve `bash` and `jq` from the developer's PATH so the suite runs off the CI image ([#956](https://github.com/max-sixty/tend/pull/956)), and `notifications-check.sh`'s sweep, cleanup, and deferral layers get behaviour tests ([#948](https://github.com/max-sixty/tend/pull/948)).
- CI smokes the pinned Codex CLI surface, and `codex_version` moves off the alpha line. ([#923](https://github.com/max-sixty/tend/pull/923))
- Weekly pin refreshes: `claude_version` 2.1.226 ([#918](https://github.com/max-sixty/tend/pull/918)), `actions/cache` v6 in the Claude action ([#919](https://github.com/max-sixty/tend/pull/919)), `astral-sh/setup-uv` v9.0.0 in the generated `tend-install-test` workflow and the `setup:` example ([#921](https://github.com/max-sixty/tend/pull/921)), `actions/setup-node` v7 in the same example ([#924](https://github.com/max-sixty/tend/pull/924)), and the refs this repo runs itself ([#920](https://github.com/max-sixty/tend/pull/920)). `setup-uv` dropped its floating major, so `@v9` doesn't resolve — future bumps in a copied `setup:` block are full-version edits.
- A `claude_version` bump now skims the claude-code CHANGELOG for slash-command and `Skill`-tool handling. ([#933](https://github.com/max-sixty/tend/pull/933))

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
