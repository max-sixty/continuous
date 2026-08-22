# Weekly integration test

Drives a real issue and a real PR against a persistent test repo, asserts
`tend-triage`, `tend-review`, and `tend-mention` ran end-to-end, and
resets the repo for the next week.

## Safety — read first

This recipe issues destructive operations (close issues, close PRs,
delete branches, push regenerated workflows to `main`) against
**exactly one** repo:
`tend-agent/tend-integration`. The literal string `tend-agent/tend-integration`
appears as `--repo` argument on every destructive call below — not a
variable. **Do not substitute a variable**, do not rename the repo, do
not run this recipe against any other repo.

If `tend-agent/tend-integration` does not exist yet, the §1 bootstrap
creates it. Once it exists, subsequent weekly runs only operate on it.

`$GITHUB_TOKEN` in the agent's env is a recognizable dummy, not the
real PAT. The harness's credential-injecting proxy swaps in the real
token on requests to GitHub hosts, so functional probes prove nothing:
`gh auth status`, clones, and API calls all succeed as `tend-agent`
even though the env value is not a working credential anywhere else.
`$CLAUDE_CODE_OAUTH_TOKEN` is likewise unavailable (absent from this
subprocess).

**Never export env credentials out of the sandbox** — in particular,
never pipe `$GITHUB_TOKEN` or `$CLAUDE_CODE_OAUTH_TOKEN` into
`gh secret set`. The exported value is the placeholder (or empty), and
the receiving repo's auth breaks. The fixture's secrets are owned by
the `integration-secrets` workflow in `max-sixty/tend`, which copies
this repo's real secrets into the fixture's `tend` environment (creating
it if missing), outside the sandbox. §1 dispatches it every run.

The bot PAT carries the `workflow` scope (so §2's self-heal push of
generated workflow files succeeds through the proxy) but **does not**
need `delete_repo` — the recipe never deletes the test repo; it resets
in place.

Run steps in order. The self-heal (§2) precedes the verification steps so
they always exercise current workflows. Any step failing jumps to §7
(reset), then §8 (report) — including §1, whose reseed failure leaves the
fixture unable to run anything, and which reports without needing the
reset since it created nothing.

## 1. Bootstrap (first run only) and reseed (every run)

Create the test repo if missing, with workflows installed on `main`
and branch protection enabled on the default branch; the creation
block is a no-op once the repo exists. Then dispatch the
`integration-secrets` workflow to seed the fixture's secrets from
outside the sandbox (see Safety above — exporting `$GITHUB_TOKEN` from
here would store the placeholder).

```bash
if ! gh repo view tend-agent/tend-integration --json name >/dev/null 2>&1; then
  gh repo create tend-agent/tend-integration --public --add-readme

  WORK=$(mktemp -d)
  gh repo clone tend-agent/tend-integration "$WORK"
  cd "$WORK"

  # The runner has no global git identity; commit needs both fields set
  # locally or `git commit` aborts and the follow-up push silently no-ops.
  git config user.email "tend-agent@users.noreply.github.com"
  git config user.name "tend-agent"

  mkdir -p .config
  cat > .config/tend.yaml <<'EOF'
bot_name: tend-agent
harness: claude
workflows:
  notifications: false
  ci-fix: false
  nightly: false
  review-runs: false
  weekly: false
EOF

  "${CLAUDE_PLUGIN_ROOT}/scripts/tend-uvx.sh" tend@latest init
  gh auth setup-git
  git add .
  git commit -m "chore: install tend (integration-test bootstrap)"
  git push origin main

  cd - >/dev/null
  rm -rf "$WORK"

  # tend's preflight requires the default branch to be protected
  # (`gh api .../branches/main --jq '.protected'` must be true). Without
  # this, every tend-* run on the repo aborts at the Security preflight
  # step. The bot owns this repo so it has admin to set protection.
  gh api -X PUT repos/tend-agent/tend-integration/branches/main/protection \
    -H "Accept: application/vnd.github+json" \
    --input - <<'EOF'
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
fi

# Reseed the fixture's secrets. Compare against the previous run ID so
# a stale earlier run is never mistaken for this dispatch.
PREV_ID=$(gh run list --repo max-sixty/tend --workflow integration-secrets \
  --limit 1 --json databaseId --jq '.[0].databaseId // empty')
gh workflow run integration-secrets --repo max-sixty/tend

RUN_ID=""
for _ in $(seq 1 24); do
  RUN_ID=$(gh run list --repo max-sixty/tend --workflow integration-secrets \
    --limit 1 --json databaseId --jq '.[0].databaseId // empty')
  [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$PREV_ID" ] && break
  sleep 5
done
{ [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$PREV_ID" ]; } \
  || { echo "integration-secrets: run never registered"; exit 1; }

for _ in $(seq 1 30); do
  read -r status conclusion < <(gh run view "$RUN_ID" --repo max-sixty/tend \
    --json status,conclusion --jq '"\(.status) \(.conclusion // "")"')
  [ "$status" = "completed" ] && break
  sleep 10
done
[ "$conclusion" = "success" ] || { echo "integration-secrets: $status/$conclusion"; exit 1; }
```

## 2. Verify the generator (self-healing)

Re-run the generator against the committed config. A diff is expected
after every tend release (the version pin moves, and the fixture
disables nightly so this is its only regeneration path) — push the
regenerated files to `main` rather than failing. Assertions: `init`
succeeds against the committed config, and is idempotent.

This runs before the verification steps so they exercise the workflows
this run just wrote, rather than the ones a release ago that it is about
to replace. It also orders the fixture's own migration: §1 keeps its
repo-level secret copies until the regenerated workflows name the `tend`
environment, so the deletion only becomes possible after this step has
run at least once against a released generator that emits it.

```bash
WORK=$(mktemp -d)
gh repo clone tend-agent/tend-integration "$WORK"
cd "$WORK"
"${CLAUDE_PLUGIN_ROOT}/scripts/tend-uvx.sh" tend@latest init
if [ -n "$(git status --porcelain)" ]; then
  git config user.email "tend-agent@users.noreply.github.com"
  git config user.name "tend-agent"
  gh auth setup-git
  git add .
  git commit -m "chore: regenerate tend workflows (weekly integration self-heal)"
  git push origin main \
    || { echo "tend-integration: push to main failed; fixture not updated"; exit 1; }
fi
"${CLAUDE_PLUGIN_ROOT}/scripts/tend-uvx.sh" tend@latest init
[ -z "$(git status --porcelain)" ] \
  || { echo "tend-integration: init not idempotent: $(git status --porcelain)"; exit 1; }
cd - >/dev/null
rm -rf "$WORK"
```

## 3. Reset to a known-clean state

Close any leftover issues/PRs from prior runs, delete any leftover
branches. `main` is never touched.

```bash
for n in $(gh issue list --repo tend-agent/tend-integration \
            --state open --json number --jq '.[].number'); do
  gh issue close "$n" --repo tend-agent/tend-integration \
    --comment "Cleaned up by weekly integration-test reset."
done

for n in $(gh pr list --repo tend-agent/tend-integration \
            --state open --json number --jq '.[].number'); do
  gh pr close "$n" --repo tend-agent/tend-integration \
    --delete-branch \
    --comment "Cleaned up by weekly integration-test reset."
done

# Any branches still hanging around (orphaned by a crashed prior run).
for b in $(gh api repos/tend-agent/tend-integration/branches \
             --jq '.[].name' | grep -v '^main$' || true); do
  gh api -X DELETE "repos/tend-agent/tend-integration/git/refs/heads/$b"
done
```

## 4. Verify tend-triage

Open a fresh test issue, wait for `tend-triage` to register and finish,
assert the bot commented.

```bash
TS=$(date -u +%Y%m%d-%H%M%S)
# Baseline the latest existing run BEFORE the trigger so a prior
# week's run is never mistaken for this one (mirrors §1).
PREV_RUN=$(gh run list --repo tend-agent/tend-integration \
  --workflow tend-triage --limit 1 \
  --json databaseId --jq '.[0].databaseId // empty')
ISSUE_URL=$(gh issue create --repo tend-agent/tend-integration \
  --title "integration-test triage $TS" \
  --body "Automated weekly integration test. The bot's reply confirms tend-triage is working; the reset step will close this.")
ISSUE=${ISSUE_URL##*/}

RUN_ID=""
for _ in $(seq 1 24); do
  RUN_ID=$(gh run list --repo tend-agent/tend-integration \
    --workflow tend-triage --limit 1 \
    --json databaseId --jq '.[0].databaseId // empty')
  [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$PREV_RUN" ] && break
  sleep 5
done
{ [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$PREV_RUN" ]; } \
  || { echo "tend-triage: workflow run never registered"; exit 1; }

for _ in $(seq 1 60); do
  read -r status conclusion < <(gh run view "$RUN_ID" \
    --repo tend-agent/tend-integration \
    --json status,conclusion --jq '"\(.status) \(.conclusion // "")"')
  [ "$status" = "completed" ] && break
  sleep 10
done
[ "$conclusion" = "success" ] || { echo "tend-triage: $status/$conclusion"; exit 1; }

COMMENTS=$(gh issue view "$ISSUE" --repo tend-agent/tend-integration \
  --json comments --jq '[.comments[] | select(.author.login == "tend-agent")] | length')
[ "$COMMENTS" -ge 1 ] || { echo "tend-triage: no bot comment on issue #$ISSUE"; exit 1; }
```

## 5. Verify tend-review

Clone, create a branch with a trivial README edit, open a PR, wait for
`tend-review` to register and finish, assert the action invoked the
Claude session (artifact present).

The `tend-review` skill is explicitly directed to exit silently on
self-authored, trivial PRs (GitHub blocks self-approval; the skill keeps
quiet when there are no concerns). So an "is there a bot review on the
PR?" assertion can't distinguish "the action never ran" from "the action
ran and stayed silent by design" — both produce zero reviews. Asserting
on the session-log artifact, which the tend harness action uploads
unconditionally on every invocation, distinguishes the two.

```bash
# Self-contained, like §6: shell state does not survive between blocks, so
# §4's $TS is already gone. The stamp only has to be unique per run, not to
# match §4's — §6 finds this PR by title prefix, never by timestamp.
TS=$(date -u +%Y%m%d-%H%M%S)

WORK=$(mktemp -d)
gh repo clone tend-agent/tend-integration "$WORK"
cd "$WORK"

git config user.email "tend-agent@users.noreply.github.com"
git config user.name "tend-agent"

BRANCH="integration-test-review-$TS"
git checkout -b "$BRANCH"
printf '\n(integration-test edit %s)\n' "$TS" >> README.md
git add README.md
git commit -m "chore: integration-test trivial edit"
gh auth setup-git
git push -u origin "$BRANCH"

# Baseline the latest existing run BEFORE the trigger so a prior
# week's run is never mistaken for this one (mirrors §1).
PREV_RUN=$(gh run list --repo tend-agent/tend-integration \
  --workflow tend-review --limit 1 \
  --json databaseId --jq '.[0].databaseId // empty')
PR_URL=$(gh pr create --repo tend-agent/tend-integration \
  --title "integration-test review $TS" \
  --body "Automated weekly integration test. The bot's review confirms tend-review is working; the reset step will close this." \
  --base main --head "$BRANCH")
PR=${PR_URL##*/}

RUN_ID=""
for _ in $(seq 1 24); do
  RUN_ID=$(gh run list --repo tend-agent/tend-integration \
    --workflow tend-review --limit 1 \
    --json databaseId --jq '.[0].databaseId // empty')
  [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$PREV_RUN" ] && break
  sleep 5
done
{ [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$PREV_RUN" ]; } \
  || { echo "tend-review: workflow run never registered"; exit 1; }

for _ in $(seq 1 60); do
  read -r status conclusion < <(gh run view "$RUN_ID" \
    --repo tend-agent/tend-integration \
    --json status,conclusion --jq '"\(.status) \(.conclusion // "")"')
  [ "$status" = "completed" ] && break
  sleep 10
done
[ "$conclusion" = "success" ] || { echo "tend-review: $status/$conclusion"; exit 1; }

# Session-log artifact presence proves the tend harness action invoked the
# Claude session. The skill may then post a review, post nothing, or
# anything in between — that's a separate concern from "did tend-review
# fire end-to-end?".
ARTIFACTS=$(gh api "repos/tend-agent/tend-integration/actions/runs/$RUN_ID/artifacts" \
  --jq '[.artifacts[] | select(.name | startswith("claude-session-logs"))] | length')
[ "$ARTIFACTS" -ge 1 ] || { echo "tend-review: no session-log artifact on run $RUN_ID"; exit 1; }

cd - >/dev/null
rm -rf "$WORK"
```

## 6. Verify tend-mention (review events)

Submit a comment review on the §5 PR that mentions the bot, and assert
the bot replied to *that* review. A review the bot leaves on its own PR is
deliberately actionable — its reviewer role speaking — so the single bot
identity can drive the full chain: review submitted → tend-mention →
reply. On current tend the chain includes the secretless relay hop (the
review event re-posted as a `repository_dispatch`), but the reply is the
assertion either way; the individual legs are visible in the run list when
this fails.

The assertion is a nonce the reply must quote, not "a bot comment appeared
after this timestamp". §5's own `tend-review` usually posts a COMMENTED
review, which is itself an actionable review event: it starts a second
tend-mention run whose reply lands in the same window and would satisfy a
timestamp-only check with the mention path completely broken.

```bash
# Self-contained: after §3's reset the only open PR is §5's, so §6 does
# not depend on a variable set in an earlier block.
PR=$(gh pr list --repo tend-agent/tend-integration --state open \
  --json number,title \
  --jq '[.[] | select(.title | startswith("integration-test review"))][0].number')
[ -n "$PR" ] || { echo "tend-mention: no integration-test PR open"; exit 1; }
NONCE="mention-$(date -u +%Y%m%d-%H%M%S)"

PREV_RUN=$(gh run list --repo tend-agent/tend-integration \
  --workflow tend-mention --limit 1 \
  --json databaseId --jq '.[0].databaseId // empty')
gh pr review "$PR" --repo tend-agent/tend-integration --comment \
  --body "@tend-agent integration test: post a PR comment quoting this token verbatim: $NONCE"

# A new tend-mention run registering distinguishes "trigger never fired"
# from "fired but no reply" when the reply assertion below fails.
RUN_ID=""
for _ in $(seq 1 24); do
  RUN_ID=$(gh run list --repo tend-agent/tend-integration \
    --workflow tend-mention --limit 1 \
    --json databaseId --jq '.[0].databaseId // empty')
  [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$PREV_RUN" ] && break
  sleep 5
done
{ [ -n "$RUN_ID" ] && [ "$RUN_ID" != "$PREV_RUN" ]; } \
  || { echo "tend-mention: workflow run never registered"; exit 1; }

# The nonce is the end-to-end assertion: event → (relay → dispatch →)
# verify → handle → reply. Comments only, deliberately: the seeding review
# above is itself authored by the bot and contains the nonce, so counting
# reviews counts the prompt and passes with the whole path broken. Asking
# for a comment and asserting on comments keeps the two apart without
# comparing ids across APIs — `gh pr view` reports a review's GraphQL node
# id, the REST list a numeric one, and they never match.
REPLIES=0
for _ in $(seq 1 60); do
  REPLIES=$(gh pr view "$PR" --repo tend-agent/tend-integration --json comments \
    --jq "[.comments[]
           | select(.author.login == \"tend-agent\" and (.body | contains(\"$NONCE\")))]
          | length")
  [ "$REPLIES" -ge 1 ] && break
  sleep 10
done
[ "$REPLIES" -ge 1 ] \
  || { echo "tend-mention: no bot comment quoting $NONCE on PR #$PR"; exit 1; }
```

## 7. Reset (always — even on failure)

Same as §3; run again to close anything created in §4/§5/§6.

```bash
for n in $(gh issue list --repo tend-agent/tend-integration \
            --state open --json number --jq '.[].number'); do
  gh issue close "$n" --repo tend-agent/tend-integration \
    --comment "Cleaned up by weekly integration-test reset."
done

for n in $(gh pr list --repo tend-agent/tend-integration \
            --state open --json number --jq '.[].number'); do
  gh pr close "$n" --repo tend-agent/tend-integration \
    --delete-branch \
    --comment "Cleaned up by weekly integration-test reset."
done

for b in $(gh api repos/tend-agent/tend-integration/branches \
             --jq '.[].name' | grep -v '^main$' || true); do
  gh api -X DELETE "repos/tend-agent/tend-integration/git/refs/heads/$b"
done
```

## 8. Report failure

If any step failed, open a labeled issue in `max-sixty/tend`. The
label is created on demand so the first failure works without prior
setup.

Assemble the body via a quoted heredoc (so bash doesn't try to evaluate
the inner backticks) and substitute the run URL through `envsubst`:

````bash
gh label create integration-test-failure --color B60205 \
  --repo max-sixty/tend 2>/dev/null || true

export RUN_URL="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
envsubst '$RUN_URL' > /tmp/integration-failure.md <<'EOF'
Run: $RUN_URL

Failed at <step>. Captured output:

```
<paste the failing command's stderr and any relevant gh run URLs from
tend-agent/tend-integration; do NOT include any secret values>
```
EOF

gh issue create --repo max-sixty/tend \
  --title "Weekly integration test failed" \
  --label integration-test-failure \
  --body-file /tmp/integration-failure.md
````

Include the test repo's failing workflow run URL in the body when
relevant (capture it during §4–§6 before §7's reset moves on).
