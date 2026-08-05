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
| `review-reviewers` | `review-reviewers.yaml` | `47 */3 * * *` | Every-3-hours analysis of adopter repo sessions |

These use the tend composite action and produce `claude-session-logs*` artifacts,
but their names don't match the `tend-*` prefix that scripts filter on by
default. `uvx tend@latest init` doesn't rewrite them either, so their
`max-sixty/tend/<harness>@X.Y.Z` pins move only when someone edits the file.

### Usage analysis

Pass extra prefixes when running token reports or listing runs so these
workflows are included:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/token-report.sh" 24 "review-"
TARGET_REPO=max-sixty/tend "${CLAUDE_PLUGIN_ROOT}/scripts/list-recent-runs.sh" "tend-" "review-"
```

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
#    version tag (`max-sixty/tend@X.Y.Z`, or `/codex@X.Y.Z`), so search the
#    bare `max-sixty/tend` token (version-agnostic; GitHub code search does
#    not index `@` or `/`, so this matches both the Claude and Codex refs).
#    `--extension yaml` is required: without it, README/CLAUDE.md/TODO.md
#    hits on `max-sixty/tend` itself crowd out tend's own workflow files
#    past the 100-result cap, dropping tend from its own consumers.json.
#    The `.github/workflows/tend-` path filter below bounds precision.
mapfile -t REPOS < <(
  gh search code 'max-sixty/tend' --extension yaml --limit 100 --json repository,path \
    | jq -r '.[] | select(.path | startswith(".github/workflows/tend-")) | .repository.nameWithOwner' \
    | sort -u
)

# 2. Resolve bot_name from each repo's .config/tend.yaml.
mkdir -p data
{
  for repo in "${REPOS[@]}"; do
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

Title each PR `chore: bump <name> to <version>`; the uv-plus-mitmproxy PR names
both.

### Action inputs

| Pin | File | Rule |
|---|---|---|
| `claude_version` | `claude/action.yaml` | track latest |
| `mitmproxy_version` | `claude/action.yaml` | track latest |
| `uv_version` | `claude/action.yaml` | move it with `mitmproxy_version` |
| `codex_version` | `codex/action.yaml` | keep it on its prerelease line; bump only to a release confirmed to run under `codex exec` |

```bash
yq '.inputs.claude_version.default' claude/action.yaml
npm view @anthropic-ai/claude-code dist-tags.latest

yq '.inputs.mitmproxy_version.default' claude/action.yaml
curl -fsS https://pypi.org/pypi/mitmproxy/json | jq -r .info.version
```

A stale `claude` binary resolves `--model opus`/`sonnet` to a superseded alias
target, so drift silently downgrades the model. Skim the claude-code CHANGELOG
between the two versions for anything touching the agent paths (first-run
onboarding, `--model` alias resolution, headless `-p` result events, Stop-hook
behavior) and note it in the PR.

`mitmproxy_version` pins the process that holds the real PAT and model
credential, so a security fix there matters here. Check anything security- or
addon-related in its CHANGELOG against the `mitmdump` flags in
`proxy/setup-sandbox.sh`, and report the comparison in the PR. `uv_version`
only launches that mitmproxy and CI smokes the two together, so it needs no
release stream of its own; move both in one PR, at whatever uv is latest then
(`curl -fsS https://pypi.org/pypi/uv/json | jq -r .info.version`).

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
its call sites, then split the PRs by who runs the result.

- **Ships to adopters** — `generator/src/tend/templates/` and `workflows.py`
  render into adopters' workflow files; `claude/action.yaml` and
  `codex/action.yaml` run in every adopter's job from the next release. One PR
  per action, its body naming what changed across the majors it crosses.
- **Ours alone** — the hand-maintained `.github/workflows/` files and
  `.config/tend.yaml`. One PR for the lot.

The generated `tend-*.yaml` show up in that grep too; their refs come from the
templates and from `.config/tend.yaml`'s `setup:`, which is where they move.

## Weekly: integration test

End-to-end check that a fresh install completes and the generated workflows
respond to a real issue and PR. Open `references/integration-test.md` and
follow the recipe in order; do not skip the cleanup step even on assertion
failure.
