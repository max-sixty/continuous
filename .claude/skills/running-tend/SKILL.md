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
| `review-reviewers` | `review-reviewers.yaml` | `47 * * * *` | Hourly analysis of adopter repo sessions |

These use the tend composite action and produce `claude-session-logs*` artifacts,
but their names don't match the `tend-*` prefix that scripts filter on by
default. `uvx tend@latest init` doesn't rewrite them either, so their
`max-sixty/tend/<harness>@X.Y.Z` pins move only when someone edits the file.
When a regen bumps the generated workflows, restamp these to match:

```bash
rg 'max-sixty/tend/' .github/workflows/   # every ref: same harness, same tag
```

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

## Weekly: bump pinned agent binaries

Every harness action installs a pinned agent binary: `claude_version` in
both `claude/action.yaml` (headless `claude -p`) and
`claude-interactive/action.yaml` (PTY), and `codex_version` in
`codex/action.yaml`. These pins are static strings nothing else moves, so
they drift behind and the harness resolves `--model opus`/`sonnet` to a stale
alias target (an old binary maps `opus` to a superseded Opus version).

```bash
# Claude: pinned defaults vs latest release
rg -A1 'claude_version:' claude/action.yaml claude-interactive/action.yaml
npm view @anthropic-ai/claude-code dist-tags.latest
```

If `latest` is newer, bump the `default:` in **both** `claude/action.yaml` and
`claude-interactive/action.yaml` (keep the two Claude pins in step) and open a PR
titled `chore: bump claude_version to <latest>`. Skim the claude-code
CHANGELOG between the two versions for anything touching the agent paths
(first-run onboarding, `--model` alias resolution, headless `-p` result
events, Stop-hook behavior) and note it in the PR.

Codex pins a prerelease on purpose (`codex_version`) and its catalog
churns, so bump it only to a release confirmed to still run under
`codex exec`, not blindly to the newest npm tag.

The bump reaches adopters at the next tend release, since their workflows
pin `max-sixty/tend@X.Y.Z`; tend's own workflows pick it up the same way.

## Weekly: integration test

End-to-end check that a fresh install completes and the generated workflows
respond to a real issue and PR. Open `references/integration-test.md` and
follow the recipe in order; do not skip the cleanup step even on assertion
failure.
