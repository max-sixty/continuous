---
name: running-tend
description: Tend-specific guidance for tend CI workflows. Adds non-standard workflow inclusion for usage analysis and repo conventions on top of the generic tend-* skills.
metadata:
  internal: true
---

# Tend CI

Repo-specific guidance for tend workflows running on tend itself. The generic
skills (`tend-running-in-ci`, `tend-review`, `tend-triage`, etc.) provide the
workflow framework; this skill adds tend conventions.

## Filing issues in other repos

Standing exception granted: file directly in agent-equipped targets without
asking permission here first. Most tend consumers in `data/consumers.json`
qualify, as do other Claude-Code-action-using repos. The default rule (open
an issue here asking permission first) still applies when the target shows no
agent signals.

## Non-standard workflows

Tend has Claude-powered workflows beyond the generated `tend-*` set:

| Workflow | File | Schedule | Purpose |
|----------|------|----------|---------|
| `review-reviewers` | `review-reviewers.yaml` | manual only (paused) | Outside-in analysis of adopter repo sessions |

`review-reviewers` runs only on `workflow_dispatch` — dispatch it as a
spot-check after a release, harness switch, or model bump, not on a cadence. The
per-repo `tend-review-runs` carries the routine loop; the workflow file's header
explains the pause.

A dispatched run's window opens at the **previous successful `review-reviewers`
run**, floored 6h back (`list_recent_runs.py`). With no cron, dispatches usually
sit further apart than that, so the floor is the normal case: the run covers the
last 6h and warns on stderr that the rest is a coverage gap. Dispatch it within
~6h of whatever you want it to see.

These use the tend composite action and produce `claude-session-logs*` artifacts,
but their names don't match the `tend-*` prefix that scripts filter on by
default. `uvx tend@latest init` doesn't rewrite them either, so their
`max-sixty/tend/<harness>@X.Y.Z` pins move only when someone edits the file.

### Usage analysis

Pass extra prefixes when running token reports or listing runs so these
workflows are included:

```bash
# Claude leaves this unset in the shell; Codex exports it.
SCRIPTS="${CLAUDE_PLUGIN_ROOT:-/home/tend-sandbox/tend-marketplace/plugins/tend-ci-runner}/scripts"
uv run --script "$SCRIPTS/token_report.py" "${HOURS:-24}" "review-"
TARGET_REPO=max-sixty/tend uv run --script \
  "$SCRIPTS/list_recent_runs.py" review-reviewers "tend-" "review-"
```

Under `review-runs`, `$HOURS` is the lookback derived from its Step 1 anchor —
passing a literal `24` there reopens the window gap that anchor closes. The
default keeps an ad-hoc invocation working.

## Labels

- `claude-behavior` — findings from `review-reviewers`
- `review-runs` — findings from `review-runs`

## Session Log Paths

Artifact paths: `-home-runner-work-tend-tend/<session-id>.jsonl`

`review-reviewers` runs produce one session log per matrix repo in
`.github/workflows/review-reviewers.yaml`.

## Nightly: verify website live data

`tend-src.com` renders its stat strip, activity feed, and currently-tending
dot entirely from the data Worker at `api.tend-src.com`. Each section *hides
itself* when its fetch fails or returns empty, so a Worker outage shows as a
blank page, not an error. Check the Worker directly — it serves the data the
site renders. See [`worker/README.md`](../../../worker/README.md).

```bash
curl -fsS https://api.tend-src.com/activity | jq '{
  prs: .prs.count, reviews: .reviews.count,
  comments: .comments.count, issues: .issues.count,
  recent: ([.prs, .issues, .reviews, .comments] | map(.recent | length) | add)
}'
curl -fsSI https://tend-src.com/ | head -1   # GitHub Pages serving the HTML
```

Healthy: both return HTTP 200, every lifetime `count` > 0, and `recent` > 0.
An empty `/currently-tending` is normal between runs — don't alarm on it.

If `/activity` is non-200, all-zero, or `recent` is 0, wait ~60s and retry
once. (Transient GitHub errors keep the last good data rather than caching
zeros, so a persistent empty is a real signal.) If it persists, file or update
**one** tracking issue (dedup by title, e.g. `website: data Worker returning
empty`) with the failing endpoint, the counts seen, and whether the bots still
have recent activity on GitHub — that localizes the fault to the Worker. The
bot can't rotate the Worker's Cloudflare-side secret itself, so leave the
diagnosis to a maintainer; `worker/README.md` covers the Worker's setup.

## Nightly: restamp the hand-maintained workflow refs

`init` rewrites only the generated `tend-*.yaml` files, so the workflows under
"Non-standard workflows" hold whatever action ref they were last given by hand.
Every release leaves them a version further behind, and a harness change made by
regenerating skips them entirely.

Run this after the regen step, whether or not it produced a PR:

```bash
rg -o --no-filename 'max-sixty/tend/[a-z-]+@[0-9.]+' .github/workflows/ | sort -u
```

One line means every workflow agrees. Two or more, restamp the hand-maintained
files onto the generated files' ref and fold it into the regen PR — same
worktree, same commit. A differing *harness* rather than a differing version is
the worse case: a config change reached the generated workflows and stopped
there, so check what else that change was supposed to carry.

## Weekly: refresh `data/consumers.json`

Public repos that have installed tend. Read by the website's data Worker
(see [`worker/README.md`](../../../worker/README.md)) to power the
currently-tending dot, activity feed, and stat strip. Needs no opt-in
because the workflow files are public.

```bash
# 1. Discover consumer repos via code search. Generated workflows pin a
#    version tag (`max-sixty/tend/claude@X.Y.Z`, or `/codex@X.Y.Z`), so
#    search the bare `max-sixty/tend` token (version-agnostic; GitHub code
#    search does not index `@` or `/`, so this matches both the Claude and
#    Codex refs).
#    `--extension yaml` is required: without it, README/CLAUDE.md/TODO.md
#    hits on `max-sixty/tend` itself crowd out tend's own workflow files
#    past the 100-result cap, dropping tend from its own consumers.json.
#    The `.github/workflows/tend-` path filter below bounds precision.
mapfile -t DISCOVERED < <(
  gh search code 'max-sixty/tend' --extension yaml --limit 100 --json repository,path \
    | jq -r '.[] | select(.path | startswith(".github/workflows/tend-")) | .repository.nameWithOwner' \
    | sort -u
)

# 2. Union with the repos already listed. Code search recall is partial — a
#    repo carrying a full set of tend-*.yaml files can return zero hits — so
#    rebuilding from the search alone deletes live consumers from the file the
#    website renders. The search finds *new* consumers; step 3 decides who stays.
mapfile -t REPOS < <(
  { printf '%s\n' "${DISCOVERED[@]}"
    jq -r '.[].repo' data/consumers.json 2>/dev/null; } | sort -u
)

# 3. Keep a repo while it still has generated tend workflows, and resolve
#    bot_name from its .config/tend.yaml. An uninstall drops out here rather
#    than by going missing from a search — but so does a repo whose `gh api`
#    call hit a 403 or a 5xx, and nothing re-adds a repo the code index can't
#    see. Never land a removal without re-checking that repo by hand.
mkdir -p data
{
  for repo in "${REPOS[@]}"; do
    workflows=$(gh api "repos/$repo/contents/.github/workflows" \
      --jq '[.[] | select(.name | startswith("tend-"))] | length' 2>/dev/null) || workflows=0
    [ "${workflows:-0}" -gt 0 ] || continue
    bot=$(gh api "repos/$repo/contents/.config/tend.yaml" --jq '.content' 2>/dev/null \
      | base64 -d 2>/dev/null \
      | yq '.bot_name // ""' 2>/dev/null)
    [ -n "$bot" ] || continue
    jq -nc --arg repo "$repo" --arg bot "$bot" '{repo: $repo, bot_name: $bot}'
  done
} | jq -s . > data/consumers.json
```

Open a PR titled `chore: refresh consumers.json` if the file changed. Skip
the PR (no diff to land) when `git status --porcelain data/consumers.json`
is empty — `git diff --quiet` returns 0 for untracked paths, so the
first-run case would no-op. Code search is 10 req/min — one call covers
the whole list.

## Weekly: bump pinned versions

Every dependency pin in the repo is in scope — no bot watches this repo, so a
pin outside the sweep drifts until it breaks. Each command below enumerates its
own ecosystem, so a pin added later shows up without anyone remembering to list
it here.

```bash
# Composite-action inputs
yq -r '.inputs | to_entries[] | select(.key | test("_version$"))
  | "\(filename) \(.key) = \(.value.default)"' */action.yaml

# Python: `==` and upper bounds freeze a version. Floors (`click>=8.0`) state
# compatibility instead and stay put — raising one only narrows adopter support.
git grep -nE '(==|~=|<=?)[0-9]' -- '*pyproject.toml'
uv lock --upgrade --dry-run

# pre-commit hook revs — the updater rewrites them, `git diff` is the report.
uv tool run pre-commit autoupdate

# npm: `Wanted` ≠ `Current` is lockfile drift (`npm update`); `Latest` ≠
# `Wanted` needs the range in package.json moved. Exits 1 when a row prints.
npm --prefix worker outdated
npm --prefix site outdated

# Versions pinned in a shell script (worktrunk, in the Codex Cloud setup)
git grep -nE '^[A-Za-z_]*VERSION=' -- '*.sh'
```

What upstream currently publishes:

```bash
npm view @anthropic-ai/claude-code dist-tags.latest
npm view @openai/codex dist-tags.latest
curl -fsS https://pypi.org/pypi/mitmproxy/json | jq -r .info.version
curl -fsS https://pypi.org/pypi/uv/json | jq -r .info.version
gh api repos/max-sixty/worktrunk/releases/latest --jq '.tag_name | ltrimstr("v")'
```

GHA `uses:` refs sweep separately, under a rule of their own — see below.
Out of scope entirely: runner images (`ubuntu-24.04`), `node-version`, and
`requires-python` are platform choices carrying their own rationale, so they
move when a reason arrives rather than on a cadence.

Default rule: move to latest and let CI decide — the table below names the pins
where CI can't. Split PRs by who runs the result, and take what fits in one
session rather than clearing a backlog at once — an unswept pin waits a week, a
swamped run finishes nothing.

- **Ships to adopters** — `claude/action.yaml` and `codex/action.yaml` run in
  every adopter's job from the next release; `generator/src/tend/templates/`
  and `workflows.py` render into their workflow files. One PR each, titled
  `chore: bump <name> to <version>` (the uv-plus-mitmproxy PR names both), its
  body naming what changed.
- **Ours alone** — everything else: pre-commit revs, the workspace dev pins,
  the `uv_build` backend, npm devDependencies, `WORKTRUNK_VERSION`, the
  hand-maintained `.github/workflows/` files and `.config/tend.yaml`. One PR
  for the lot.
- **A major** — an npm `Latest` ≠ `Wanted`, or a new mitmproxy/uv major — gets
  its own PR from either bucket, its body reporting the migration notes.

### Pins with rules of their own

| Pin | File | Rule |
|---|---|---|
| `claude_version` | `claude/action.yaml` | npm's `latest` dist-tag, not `stable` |
| `mitmproxy_version` | `claude/action.yaml` | move the root `pyproject.toml` `==` pin with it and `uv lock` |
| `uv_version` | both harness `action.yaml` files | move both defaults together, with `mitmproxy_version` |
| `codex_version` | `codex/action.yaml` | `latest`; `alpha` only for a fix not yet released |
| `uv_build` | `generator/pyproject.toml` | its range must contain the uv doing the build; a stale one only warns during `uv build`, so only this sweep catches it |
| `WORKTRUNK_VERSION` | `.config/codex-cloud/environment.sh` | nothing in CI runs the script, and it dies under `set -euo pipefail` — confirm the release still ships `worktrunk-installer.sh` and that `wt config approvals add --yes` still records approvals without a TTY |

A stale `claude` binary resolves `--model opus`/`sonnet` to a superseded alias
target, so drift silently downgrades the model. Skim the claude-code CHANGELOG
between the two versions for anything touching the agent paths (first-run
onboarding, `--model` alias resolution, headless `-p` result events, Stop-hook
behavior, slash-command or Skill-tool handling) and note it in the PR.

`mitmproxy_version` pins the process that holds the real PAT and model
credential, so a security fix there matters here. Check anything security- or
addon-related in its CHANGELOG against the `mitmdump` flags in
`proxy/setup_sandbox.py`, and report the comparison in the PR. `uv_version`
also supplies the agent fallback in both harnesses. CI smokes the installer and
proxy together, so move uv and mitmproxy in one PR.

For `codex_version`, CI's `test-codex-surface` job installs whatever is pinned
and asserts the CLI surface the action depends on, so a bump that breaks it
fails on its own PR. No `OPENAI_API_KEY` reaches this repo's runs, so a live
agent session stays unverified — skim the codex CHANGELOG across the bump for
model availability, sandbox behavior, and `--output-last-message`, and note what
you find in the PR.

### `uses:` refs

```bash
git grep -hoE 'uses: [^ ./][^ @]*@[^ ]+' -- ':!generator/tests' ':!*.md' \
  | sed 's/uses: //' | grep -v '^max-sixty/tend/' | sort -u \
  | while IFS='@' read -r action pin; do
      latest=$(gh api "repos/$action/releases/latest" --jq .tag_name 2>/dev/null) \
        || { printf '%-30s %-9s -> no releases; read its tags\n' "$action" "$pin"; continue; }
      case "$pin" in "$latest" | "${latest%%.*}") continue ;; esac
      printf '%-30s %-9s -> %s\n' "$action" "$pin" "$latest"
    done
```

An action listed twice is pinned at two majors: refs move when someone needs a
behavior from one of them, never in a sweep. `git grep` each drifted action for
its call sites, then split the PRs by the buckets above — a ref that ships to
adopters gets its own, its body naming what changed across the majors it
crosses.

The generated `tend-*.yaml` show up in that grep too; their refs come from the
templates and from `.config/tend.yaml`'s `setup:`, which is where they move.

## Weekly: integration test

End-to-end check that a fresh install completes and the generated workflows
respond to a real issue and PR. Open `references/integration-test.md` and
follow the recipe in order; do not skip the cleanup step even on assertion
failure.
