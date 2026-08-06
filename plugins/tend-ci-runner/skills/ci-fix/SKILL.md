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

#### Sibling sessions on the same failed run

A consumer that keeps a nightly-failure tracker issue opens it before this session starts, and `tend-triage` fires on that issue within seconds of `tend-ci-fix` firing on the run. Both sessions then diagnose the same failure without knowing the other exists. The searches above can't see it: they match `ci-fix: in:title`, this skill's own artifacts, while the tracker is titled whatever the consumer's template says.

Search by the **failed run id** instead, across any state — a sibling may have already diagnosed and closed:

```bash
RUN_ID=<failed run id>
gh api "repos/$GITHUB_REPOSITORY/issues?state=all&sort=updated&since=$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)&per_page=100" \
  --jq "[.[] | select(.pull_request == null)
             | select((.body // \"\") | contains(\"/runs/$RUN_ID\"))] | map({number, title, state})"
```

Three things that recipe depends on:

- **Assign `RUN_ID`.** Unset, the filter degrades to `contains("/runs/")` and matches any issue citing any run — every session would defer to an unrelated thread.
- **`select(.pull_request == null)`.** This endpoint returns pull requests as well as issues, and step 3's PR body links the failed run, so a fix PR matches the same filter. PRs are already handled at the top of step 1, and `Fixes #<n>` aimed at a PR number is silently ignored.
- **The list endpoint, not `gh search issues`.** Search indexing lags minutes behind; the sibling started seconds ago.

`since` and `sort=updated` both key on update time — a tracker reused across a failure streak (`update_existing: true`) has a stale creation date, and the endpoint's default `sort=created` would let it fall off the single page of 100 on a busy repo.

A hit means a sibling owns this failure's issue thread. Then:

- **Don't open your own diagnostic issue** (3a/3b) — comment the diagnosis on that issue, and only if it adds something the sibling didn't post.
- **Check what the sibling already did** before repeating it — a rerun it triggered is already in flight, and both sessions polling the same job to completion doubles the cost for one result.
- **A fix PR is still yours to open** if the cause is durable; reference the sibling issue with `Fixes #<n>`.

This is defer-on-discovery, not a race-free gate — the two sessions start ~2 s apart. It works because the tracker exists before either one does.

### 2. Diagnose and fix

1. Get failure logs: `gh run view <run-id> --log-failed`
2. Identify the failing job and root cause — don't just fix the symptom
3. Search for the same pattern elsewhere in the codebase
4. Reproduce locally using test commands from the project's CLAUDE.md
5. Fix at the right level (shared helper > per-file fix)

### 3. Create PR

Re-check for existing fix PRs (one may have been created while you worked).

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

Instead, open an issue with the diagnosis and close it immediately — unless step 1 found a sibling's issue for this run, in which case comment there. The closure records "diagnosed, no further action" while keeping the analysis discoverable and off the commit timeline. Apply the `tend-outage` label — the workflow-level `if:` in `tend-triage` and `tend-mention` skip labelled issues, suppressing the no-op cascade runs (`opened` → silent-exit; `closed`-comment → silent-exit) that would otherwise fire on every transient tracker:

```bash
gh label create tend-outage --description "Tracks bot outage incidents" --color "d93f0b" 2>/dev/null || true
gh issue create --title "ci-fix: transient failure on <run-id>" --label tend-outage --body-file /tmp/diagnosis.md
gh issue close <issue-number> --reason "not planned" --comment "Transient — closing as diagnosed."
```

Use this path only when the evidence points to ephemeral infrastructure, not anything the project's code does. Signals (examples, not a checklist): the same code path passed on recent prior runs with no relevant change; the failure shape is filesystem/network-level; an upstream status incident matches the timing and components. Weigh the evidence rather than matching the list.

#### Repeat-occurrence escalation

Before applying the transient path, check whether the same failure shape has already been classified transient recently. The single-shot criteria above don't catch an intermittent upstream regression — each rerun-pass reinforces the wrong classification.

```bash
BOT_LOGIN=$(gh api user --jq '.login')
gh issue list --state all --author "$BOT_LOGIN" --search "ci-fix: in:title" \
  --json number,title,createdAt \
  --jq "[.[] | select(.createdAt >= (now - 7*86400 | todateiso8601))]"
```

Match by failure-shape keyword (e.g. `rustup-init`, `composer connect timeout`, `docker pull rate limit`) — not by job name. The same root cause can surface on multiple jobs.

If 2+ prior issues match the current failure shape within the past 7 days, escalate to durable: a fault that re-fires every 1–3 days is not transient even when individual reruns pass. Search for an upstream-documented workaround (`gh issue search` against the action's repo, the action's README, GitHub Community threads) and apply it. If no upstream workaround is documented, open a fix PR proposing a minimal mitigation (pin runner image, skip the affected leg, disable the relevant cache layer) and link the upstream tracking issue.

If you can't tell whether it's transient, treat it as durable and create a fix PR.

Skip step 4 — there's no PR to monitor.

### 3b. Diagnosis without a fix (durable causes)

If the diagnosis identifies a durable root cause but a safe fix can't be produced — the cause is in an external system the bot can't change, the fix requires judgment the bot shouldn't make unilaterally, or an attempted fix didn't validate locally — leave a tracking issue. Without one, a durable failure that the bot can't fix lives only on the workflow-run page and is invisible in the issues list.

Leave the issue **open**. A subsequent fix PR closes it via `Fixes #<n>` in the PR body (see step 1 — search for a matching open tracking issue before opening the fix PR). This mirrors the consumer-side `create-issue-on-nightly-failure` pattern and gives maintainers a durable "still broken" signal until a fix ships.

**Dedup first.** If step 1's run-id search found a sibling's issue, comment there and stop. Otherwise search for an open tracking issue covering the same failure shape; if one exists, comment with the new run link rather than opening a duplicate. Match by failure shape (workflow name + diagnostic snippet), not run ID — a *recurrence* of the same cause carries a different run ID and won't dedup on it:

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
