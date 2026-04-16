<!-- Shared gates and evidence framework for review-reviewers and review-runs skills. -->
<!-- Symlinked into each skill directory; changes here apply to both. -->

## Confidence and magnitude gates

Before creating a PR, every finding must pass two gates.

### Gate 1: Confidence — is this a real problem?

| Evidence level | Meaning | Minimum occurrences to act |
|---|---|---|
| **Critical** | Clearly wrong outcome (closed wrong issue, merged broken code, deleted user data) | 1 |
| **High** | Consistent pattern across multiple sessions | 2–3 |
| **Medium** | Plausible problem seen once, could be noise | 5+ |
| **Low** | Nitpick or stylistic preference | Do not act |

Occurrences include both the current analysis **and** historical evidence recorded by prior runs.
Each skill defines where that evidence lives — see the calling skill's "Evidence accumulation"
section.

If a finding doesn't meet the threshold, **skip it** — don't create a PR, don't create an issue,
don't comment. Record it in the evidence store so it can accumulate over future runs.

### Gate 2: Magnitude — is the fix proportionate?

| Change type | Examples | Evidence bar |
|---|---|---|
| **Removal / simplification** | Remove confusing sentence, delete dead guidance | Low (1 occurrence is enough) |
| **Targeted fix** | Fix a specific incorrect instruction, add a missing step | Normal (use Gate 1 thresholds) |
| **New paragraph or section** | Add explanation of a concept, new workflow guidance | High (need 3+ occurrences showing the gap) |
| **Structural change** | Reorganize a skill, add a new skill file, change workflow | Very high (need 5+ occurrences or a critical failure) |

**The larger the change, the more evidence required.** A one-line simplification needs less
justification than a new paragraph. Prefer small, targeted fixes over broad rewrites.

### Structural vs. stochastic failures

Before applying the gates, classify each failure by asking: **did the bot have a decision point?**

- **Structural**: no decision point — the same conditions produce the same failure every time,
  regardless of how the bot approached the task. E.g., "the checkout differs between
  `pull_request_target` and `issue_comment` events, so grepping always finds stale content." One
  clear occurrence is sufficient evidence for a targeted fix.

- **Stochastic**: the failure is a probabilistic model behavior — e.g., "the model was too
  agreeable when challenged" or "the model forgot to check X." The same model might handle the
  next identical situation correctly without any guidance change. These need significantly more
  evidence (5+ occurrences) because adding guidance for a one-off stochastic lapse adds noise
  that can degrade performance on other tasks.

The test: "If I replayed this exact scenario 10 times, would the failure occur every time
(structural) or only sometimes (stochastic)?" When in doubt, classify as stochastic and wait for
more evidence.

### Applying the gates

For each finding, state:
1. The evidence level and occurrence count (current + historical)
2. Whether the failure is structural or stochastic
3. The proposed change type
4. Whether it passes both gates

Only proceed to act on findings that pass both gates.

## Finding format

Each run appends findings to the skill's evidence store under a `## Run <run-id>` heading.
**Always derive the run ID, timestamp, and repo from the CI environment — never hand-type them.**
Past sessions have filled the `<run-id>` placeholder with fabricated round numbers (e.g.
`24294000000`) when the skill didn't explicitly point at `$GITHUB_RUN_ID`, producing dead
link-anchors in the evidence log.

```bash
RUN_ID="$GITHUB_RUN_ID"
TIMESTAMP=$(date -u -Iseconds | sed 's/+00:00/Z/')
REPO="$GITHUB_REPOSITORY"
```

When composing the findings file, either interpolate the values with an unquoted heredoc (so
`$RUN_ID` expands) or read them first and write the literal values into the file:

```
## Run <RUN_ID> — <TIMESTAMP>

### <short description>
- **Evidence level**: Medium
- **Occurrences this run**: 1
- **Run ID**: <RUN_ID>
- **Workflow**: https://github.com/<REPO>/actions/runs/<RUN_ID>
- **Session**: <session file>
- **Detail**: <brief description of what was observed>
```

Each run gets its own heading so future runs can count prior occurrences and trace incidents to
session logs.

When a historical entry looks like it might match a current finding, **download and investigate the
linked workflow's session logs** — don't rely on the summary text alone, which lacks sufficient
context to judge relatedness. Trace the original decision chain in the session JSONL to confirm the
historical case is genuinely the same pattern, not just superficially similar.
