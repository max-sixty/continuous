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

Occurrences include both the current analysis **and** historical evidence from the tracking issue
(see [Evidence accumulation](#evidence-accumulation)).

If a finding doesn't meet the threshold, **skip it** — don't create a PR, don't create an issue,
don't comment. Record it in the tracking issue so it can accumulate evidence over future runs.

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

Before applying the gates, classify each failure:

- **Structural**: the failure has a deterministic cause that guidance can prevent — e.g., "the
  checkout differs between `pull_request_target` and `issue_comment` events, so grepping always
  finds stale content." These failures will recur every time the same conditions arise. One clear
  occurrence is sufficient evidence for a targeted fix.

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

## Evidence accumulation

Each run only sees a window of CI sessions, but patterns may emerge over days or weeks. Use a
**monthly tracking issue** to accumulate evidence across runs.

The tracking issue label should match the calling skill — e.g., `review-reviewers-tracking` or
`review-runs-tracking`.

### Finding or creating the tracking issue

```bash
MONTH=$(date +%Y-%m)
TRACKING_LABEL="<skill-name>-tracking"  # set by the calling skill
gh issue list --state open --label "$TRACKING_LABEL" \
  --json number,title --jq ".[] | select(.title | contains(\"$MONTH\"))"
```

If none exists for the current month, create one:

```bash
cat > /tmp/tracking-body.md << 'EOF'
Monthly tracking issue for below-threshold findings. Each run appends findings as a comment. Future runs read these to build cumulative evidence.

**Do not close manually** — a new issue is created each month.
EOF
gh issue create \
  --title "$TRACKING_LABEL: $MONTH" \
  --label "$TRACKING_LABEL" \
  -F /tmp/tracking-body.md
```

### Reading historical evidence

Before applying the gates, read the current tracking issue's comments to find prior observations
that overlap with current findings:

```bash
TRACKING_NUMBER=<number from above>
gh issue view "$TRACKING_NUMBER" --json comments \
  --jq '.comments[] | {author: .author.login, body: .body}'
```

Also check last month's tracking issue (if it exists) for recent carry-over.

When a historical entry looks like it might match a current finding, **download and investigate the
linked workflow's session logs** — don't rely on the summary text alone, which lacks sufficient
context to judge relatedness. Trace the original decision chain in the session JSONL to confirm the
historical case is genuinely the same pattern, not just superficially similar.

### Recording below-threshold findings

After analysis, find **the bot's existing comment** on the tracking issue and **append** new
findings to it. If no bot comment exists yet, create one. This avoids notification spam from
frequent runs.

```bash
REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
BOT_LOGIN=$(gh api user --jq '.login')
EXISTING_COMMENT=$(gh api "repos/$REPO/issues/$TRACKING_NUMBER/comments" \
  --jq "[.[] | select(.user.login == \"$BOT_LOGIN\")] | last | .id // empty")
```

If `EXISTING_COMMENT` is non-empty, download existing body, append new findings, then PATCH. Never
replace the body — prior entries contain per-run evidence needed for gate evaluation.

```bash
gh api "repos/$REPO/issues/comments/$EXISTING_COMMENT" --jq '.body' > /tmp/existing.md
cat /tmp/existing.md /tmp/findings.md > /tmp/combined.md
gh api "repos/$REPO/issues/comments/$EXISTING_COMMENT" -X PATCH -F body=@/tmp/combined.md
```

Otherwise create a new comment.

Format each finding under a `## Run <run-id>` heading:

```
## Run <run-id> — <ISO timestamp>

### <short description>
- **Evidence level**: Medium
- **Occurrences this run**: 1
- **Run ID**: <run-id>
- **Workflow**: https://github.com/{owner}/{repo}/actions/runs/<run-id>
- **Session**: <session file>
- **Detail**: <brief description of what was observed>
```

Each run gets its own heading so future runs can count prior occurrences and trace incidents to
session logs.
