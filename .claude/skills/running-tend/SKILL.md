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

## Non-standard workflows

Tend has Claude-powered workflows beyond the generated `tend-*` set:

| Workflow | File | Schedule | Purpose |
|----------|------|----------|---------|
| `review-reviewers` | `review-reviewers.yaml` | `47 * * * *` | Hourly analysis of adopter repo sessions |

These use the `tend@v1` action and produce `claude-session-logs*` artifacts,
but their names don't match the `tend-*` prefix that scripts filter on by
default.

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

`review-reviewers` runs produce 3 session logs per run (one per matrix repo:
`max-sixty/worktrunk`, `max-sixty/tend`, `PRQL/prql`).
