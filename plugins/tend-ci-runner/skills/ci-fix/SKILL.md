---
name: ci-fix
description: Debug and fix failing CI on the default branch. Use when CI fails on main.
argument-hint: "[run-id and context]"
metadata:
  internal: true
---

# Fix CI on Default Branch

CI has failed on the default branch. Diagnose the root cause, fix it, and create a PR.

**Failed run:** $ARGUMENTS

## Workflow

### 0. Load environment skills

Load `/tend-ci-runner:running-in-ci` first — it contains CI security rules, polling conventions, and comment formatting guidance. It will also prompt you to load any repo-specific skills (e.g., `running-tend`).

### 1. Check for existing fixes

List recent PRs (open and closed) and check whether any already address the same failure — a prior bot attempt, a prior bot fix a maintainer rejected, or a maintainer's in-flight fix under any branch name.

```bash
gh pr list --state all --limit 30 --json number,title,state,author,headRefName,body,closedAt
```

Match by **failure shape** — the diagnostic snippet in the bot's PR body, or the diff for a maintainer-authored PR — not branch name; branch names encode run IDs and never repeat.

- If an existing **open** PR addresses the same failure, comment on it linking the new run and stop.
- If a **closed** PR with a maintainer rejection covers the same failure, exit silently; check the closure comment for the rationale before referencing it. Re-deriving the same fix forces a maintainer to close it twice.

Also check for open tracking issues left by a prior unfixable diagnosis (see 3b) — if one matches the current failure shape, the fix PR you eventually open should reference it via `Fixes #<n>` so the issue closes when the PR merges:

```bash
BOT_LOGIN=$(gh api user --jq '.login')
gh issue list --state open --author "$BOT_LOGIN" --search "ci-fix: in:title" \
  --json number,title,body --limit 10
```

### 2. Diagnose and fix

1. Get failure logs: `gh run view <run-id> --log-failed`
2. Identify the failing job and root cause — don't just fix the symptom
3. Search for the same pattern elsewhere in the codebase
4. Reproduce locally using test commands from the project's CLAUDE.md
5. Fix at the right level (shared helper > per-file fix)

### 3. Create PR

Re-check per **Dedup recheck immediately before `gh pr create`** in `running-in-ci` — against both sources — and re-run step 1's PR query, which is author-agnostic where that section's is filtered to the bot's own PRs. A sibling or a maintainer may have opened a fix PR while you worked, and the fix may have landed as a commit on the default branch your checkout predates. If the failure no longer reproduces on the fetched default branch, don't open the PR.

```bash
git checkout -b fix/ci-<run-id>
git add <files>
git commit -m "fix: <description>"
git push -u origin fix/ci-<run-id>
```

Create the PR with `gh pr create`. PR body format:

```
## Problem
[What failed and the root cause]

## Solution
[What was fixed and why this is the right level]

## Testing
[How the fix was verified]

---
Automated fix for [failed run](run-url)
```

### 3a. Diagnosis without a fix (transient causes)

If the diagnosis identifies the failure as transient — runner-disk corruption, an isolated network blip, an upstream incident that has since resolved — there is no fix PR to create. Don't post the diagnosis as a commit comment (it surfaces on whatever commit triggered CI, including release commits where it's visibly off-topic).

Use this path only when the evidence points to ephemeral infrastructure, not anything the project's code does. Signals (examples, not a checklist): the same code path passed on recent prior runs with no relevant change; the failure shape is filesystem/network-level; an upstream status incident matches the timing and components. If you can't tell whether it's transient, treat it as durable — create a fix PR, or follow 3b if a safe fix can't be produced.

#### Repeat-occurrence escalation

Before filing the tracker below, check whether the same failure shape has already been classified transient recently. The single-shot criteria above don't catch an intermittent upstream regression — each rerun-pass reinforces the wrong classification.

```bash
BOT_LOGIN=$(gh api user --jq '.login')
gh issue list --state all --label tend-outage --author "$BOT_LOGIN" \
  --search "ci-fix: transient failure in:title" \
  --json number,title,body,createdAt \
  --jq "[.[] | select(.createdAt >= (now - 7*86400 | todateiso8601))]"
```

Both filters are load-bearing: the label keeps 3b's durable trackers out, and the title keeps out the `report_failure.py` **"Bot temporarily unavailable"** issues, which carry the same label and author and vastly outnumber these — without it the first page is all outage rows and the count reads zero.

Match by failure-shape keyword against the issue body (e.g. `rustup-init`, `composer connect timeout`, `docker pull rate limit`) — not by job name. The same root cause can surface on multiple jobs.

If the current failure shape has 2+ prior occurrences on separate days within the past 7, escalate to durable: a fault that keeps coming back within a week is not transient even when individual reruns pass. Count occurrences, not trackers — the same root cause taking down several jobs in one afternoon files several trackers and is still one occurrence.

An escalated fault still reruns green, so a mitigation buys back runner time, not correctness — the compute-only bar in **Weighing a Fix** (`running-in-ci`) applies. Open a fix PR proposing a knob-sized mitigation (pin the runner image, skip the affected leg, disable the relevant cache layer), preferring an upstream-documented workaround — `gh issue search` against the action's repo, the action's README, GitHub Community threads — and linking the upstream issue if the search surfaced one. If the fault has no knob-sized mitigation, treat it as a durable cause without a safe fix and follow 3b.

#### File the transient tracker

If the failure stays classified transient, open an issue with the diagnosis and close it immediately. The closure records "diagnosed, no further action" while keeping the analysis discoverable and off the commit timeline. Apply the `tend-outage` label — the workflow-level `if:` in `tend-triage` and `tend-mention` skip labelled issues, suppressing the no-op cascade runs (`opened` → silent-exit; `closed`-comment → silent-exit) that would otherwise fire on every transient tracker:

```bash
gh label create tend-outage --description "Tracks bot outage incidents" --color "d93f0b" 2>/dev/null || true
gh issue create --title "ci-fix: transient failure on <run-id>" --label tend-outage --body-file /tmp/diagnosis.md
gh issue close <issue-number> --reason "not planned" --comment "Transient — closing as diagnosed."
```

Skip step 4 — there's no PR to monitor.

### 3b. Diagnosis without a fix (durable causes)

If the diagnosis identifies a durable root cause but a safe fix can't be produced — the cause is in an external system the bot can't change, the fix requires judgment the bot shouldn't make unilaterally, or an attempted fix didn't validate locally — leave a tracking issue. Without one, a durable failure that the bot can't fix lives only on the workflow-run page and is invisible in the issues list.

Leave the issue **open**. A subsequent fix PR closes it via `Fixes #<n>` in the PR body (see step 1 — search for a matching open tracking issue before opening the fix PR). This mirrors the consumer-side `create-issue-on-nightly-failure` pattern and gives maintainers a durable "still broken" signal until a fix ships.

**Dedup first.** Search for an open tracking issue covering the same failure shape; if one exists, comment with the new run link rather than opening a duplicate. Match by failure shape (workflow name + diagnostic snippet), not run ID — each run ID is unique and won't dedup:

```bash
BOT_LOGIN=$(gh api user --jq '.login')
gh issue list --state open --author "$BOT_LOGIN" --search "ci-fix: in:title" \
  --json number,title,body --limit 10
```

If an open tracking issue matches:

```bash
gh issue comment <issue-number> --body-file /tmp/recurrence.md
```

Otherwise, open a new tracking issue. Use a title prefix that future runs can search on (`ci-fix: <workflow-name> failing`) with a short root-cause suffix for human readability:

```bash
gh issue create \
  --title "ci-fix: <workflow-name> failing — <short root cause>" \
  --body-file /tmp/diagnosis.md
```

Body format:

```
## Failure

[Workflow name + link to failed run]

## Diagnosis

[Root cause — what failed and why]

## Why no fix was produced

[What was attempted, what blocks an automated fix]
```

Skip step 4 — there's no PR to monitor.

### 4. Monitor CI

Wait for CI per **CI Monitoring** in `running-in-ci` (loaded in step 0).
