---
name: triage
description: Triages new GitHub issues — classifies, reproduces bugs, attempts conservative fixes, and comments. Use when a new issue is opened and needs automated triage.
argument-hint: "[issue number]"
metadata:
  internal: true
---

# Issue Triage

Triage a newly opened GitHub issue.

**Issue to triage:** $ARGUMENTS

## Step 1: Setup

Load `/tend-ci-runner:running-in-ci` first (CI environment rules, security). It will also prompt you to load any repo-specific skills (e.g., `running-tend`) — do so before proceeding.

Follow the AD FONTES principle throughout: reproduce before fixing, evidence before speculation, test before committing.

## Step 2: Read and classify the issue

```bash
gh issue view $ARGUMENTS --json title,body,labels,author
```

Classify into one of:

- **Bug report** — describes unexpected behavior, includes steps to reproduce or error output. Descriptions of changed behavior ("no longer works", "used to work") strongly signal a bug even with a terse body.
- **Feature request** — asks for new functionality or behavior changes
- **Question** — asks how to do something or how something works
- **Other** — doesn't fit the above categories

## Step 3: Check for duplicates

*Skip for questions and other.*

```bash
# Search open issues for similar problems
gh issue list --state open --json number,title,labels --limit 50

# Check for existing fix branches and PRs
git branch -r --list 'origin/fix/*'
gh pr list --state open --json number,title,headRefName --limit 50
```

If a duplicate or existing fix is found, note it for the comment in step 7. Don't create a duplicate fix.

## Step 4: Investigate existing functionality

*Feature requests only.*

Search the codebase to check whether the requested feature already exists.

1. **Extract the core ask** — What specific behavior does the requester want?
2. **Search for implementations** — Grep for relevant function names, config keys, CLI flags, and domain terms.
3. **Read key files** — If searches find hits, read the relevant source to understand what already exists and how it works.
4. **Check docs and help text** — Look for user-facing documentation of the feature.

Record what you found (or didn't find) for use in step 7.

## Step 5: Reproduce the bug

*Bug reports only.*

1. **Understand the report** — What command was run? What was expected? What actually happened?
2. **Find relevant code** — Search the codebase for the functionality described
3. **Write a failing test** — Add a test to the appropriate *existing* test file that demonstrates the bug. Don't create new test files.
4. **Run the test** to confirm it fails. Use the project's test commands from CLAUDE.md.

If the test passes (bug may already be fixed), note this for the comment.

If you cannot reproduce the bug (unclear steps, environment-specific, etc.), note what you tried and skip to step 7. Do NOT proceed to Step 6 without a failing test — a fix without reproduction evidence is not a conservative fix.

## Step 6: Fix (conservative)

*Bug reports only.*

**CRITICAL — gate check before proceeding:**

You MUST have a failing test from Step 5 before writing any fix. If you skipped the test (couldn't write one, environment-specific bug, etc.), do NOT attempt a fix — go directly to Step 7 and use the "Reproduction test only" or "Could not reproduce" comment template.

**Only attempt a fix if ALL of these conditions are met:**

- Bug is clearly reproducible (test written in Step 5 fails)
- Root cause is understood
- Fix is localized (1-3 files changed)
- Confident the fix is correct

### Skill text fixes

When the bug is about bot behavior (e.g., "bot didn't use links", "bot posted wrong format"), the root cause is often a skill/prompt compliance issue, not missing code. Before adding guidance to a skill:

1. **Check ALL co-loaded skills** — Skills loaded together in the same workflow share context. If the guidance already exists in a co-loaded skill, the issue is behavioral compliance, not missing instructions.
2. **Don't duplicate guidance across skills.**

### Don't "fix" tests by adding skip guards

If the proposed change removes coverage for the failing scenario instead of restoring the assertion, stop. Smell patterns: a newly-added early-return at the top of the test (`let Ok(_) = X else { return };`, `if !path.exists() { return; }`), a fresh `#[ignore]`, a newly-inserted `skipIf` / `pytest.skip` keyed on the failing condition. The fix belongs in production code or test setup, not in a guard that makes the test bail when the bug fires.

### Defer to in-flight same-root-cause PRs

Step 3's duplicate check catches identical fixes. It misses the *same root cause class, different surface* pattern: several failing tests share one underlying cause, and an outstanding PR fixes some of them but not the one being triaged. When the triage analysis itself names an existing PR as same-root-cause, that's the signal to wait for it to merge and re-run, or to mirror its approach for the remaining sites — not to open a parallel narrow workaround.

### If fixing

1. Fix the root cause (not just the symptom)
2. Confirm the reproduction test now passes — that targeted pass plus a clean compile is enough local confidence to ship. Leave the comprehensive suite to PR CI per `/tend-ci-runner:running-in-ci`'s "End the turn only when work is shipped"; backgrounding a long suite before push pushes the agent into mid-wait `end_turn` and the deliverable never ships.
3. Create branch, commit, push, and create PR:
   ```bash
   git checkout -b fix/issue-$ARGUMENTS
   git add -A
   git commit -m "fix: <description>

   Closes #$ARGUMENTS"
   git push -u origin fix/issue-$ARGUMENTS
   ```
   Compose the body with the Write tool at `/tmp/pr-body.md` — the fill-ins below are freeform prose that routinely carries markdown inline code, which bash executes inside a double-quoted `--body`:

   ```markdown
   ## Problem
   [What the issue reported and the root cause]

   ## Solution
   [What was fixed and why]

   ## Testing
   [How the fix was verified — mention the reproduction test]

   ---
   Closes #$ARGUMENTS — automated triage
   ```

   ```bash
   gh pr create --title "fix: <description>" --body-file /tmp/pr-body.md
   ```
4. Wait for CI per **CI Monitoring** in `/tend-ci-runner:running-in-ci`.

### If reproduction test works but fix is not confident

Commit just the failing test on a reproduction branch and open a PR:

```bash
git checkout -b repro/issue-$ARGUMENTS
git add -A
git commit -m "test: add reproduction for #$ARGUMENTS"
git push -u origin repro/issue-$ARGUMENTS
```

Compose the body with the Write tool at `/tmp/pr-body.md`:

```markdown
## Context
Adds a failing test that reproduces #$ARGUMENTS. The fix is not yet included — this PR captures the reproduction so a maintainer can investigate.

---
Automated triage for #$ARGUMENTS
```

```bash
gh pr create --title "test: reproduction for #$ARGUMENTS" --body-file /tmp/pr-body.md
```

Note the PR number for the comment.

## Step 7: Comment on the issue

**Recheck before posting** per **Recheck Before Posting** in `/tend-ci-runner:running-in-ci` — triage can take minutes, so re-fetch the issue and skip any point a new human comment or a sibling tend workflow already covered.

Always comment via `gh issue comment`. Keep it brief, polite, and specific. A maintainer will always review — never claim the issue is fully resolved by automation alone.

**Drop the maintainer-deferral closer** ("a maintainer will review", "I'll leave it for a maintainer to evaluate and prioritize", and the like) **when `author_association` is `OWNER`, `MEMBER`, or `COLLABORATOR`** — deferring to a maintainer reads as absurd when the reporter is one. Keep it otherwise, where it signals the action isn't authoritative.

```bash
gh api "repos/$GITHUB_REPOSITORY/issues/$ARGUMENTS" --jq '.author_association'
```

**Stay within what you verified.** State facts you found in the codebase — don't characterize something as "known" unless you find prior issues or documentation about it. Don't speculate beyond the code you read.

**Report the finding, not the search.** For a feature that plainly doesn't exist yet, "I searched the codebase and didn't find an existing implementation" only restates what the requester already knows. Lead with what they don't: the closest related code, where the change would slot in, or a tradeoff worth flagging. Mention searching only when the result is itself the news (e.g. the capability turns out to be computed internally but never surfaced).

**Apply the project lens** (priority 2 in the system prompt — project excellence outranks individual help). Before replying, ask what the issue reveals beyond this one reporter. If the underlying problem affects many users or the project's health — a false positive on a released artifact, a broken install path, a bad default, a misleading doc — foreground the durable, project-level fix, not just the individual's workaround. Take the pro-project action available to you (open a fix PR, or file/link a tracking issue for the durable fix) rather than handing the reporter only a personal stopgap. Deferring *prioritization* of the durable fix to a maintainer is fine; burying it under personal workarounds is not.

### Reply examples

These illustrate the tone and what each kind of reply should cover; they aren't text to paste. Match the situation, then write a reply that fits the actual issue — vary the wording and drop anything that doesn't apply.

#### Fix PR created

> Thanks for reporting this. I was able to reproduce the issue and identified the root cause: [one-sentence explanation].
>
> I've opened #PR_NUMBER with a fix. A maintainer will review it shortly.

#### Reproduction test only (no fix attempted)

> Thanks for reporting this. I was able to reproduce the issue — #PR_NUMBER adds a failing test that demonstrates the bug.
>
> Root cause appears to be [brief explanation if known, or "still under investigation"]. A maintainer will take a closer look.

#### Could not reproduce

> Thanks for reporting this. I tried to reproduce this but wasn't able to with the information provided.
>
> Could you share [specific information needed — exact command, config file, OS, shell, etc.]? That would help narrow it down.
>
> A maintainer will also take a look.

#### Bug already fixed

> Thanks for reporting this. I looked into this and it appears the behavior described may already be fixed on the default branch (the relevant test passes).
>
> Could you confirm which version you're running? If you're on an older release, updating should resolve this. A maintainer will confirm.

#### Feature may already exist

> Thanks for the suggestion. It's possible that [existing feature — specific behavior, config/flag] already does what you're looking for: [brief description of how it works].
>
> If that's not quite what you had in mind, could you clarify what additional behavior you're looking for? A maintainer will take a look either way.

#### Feature does not exist

> Thanks for the suggestion. There's no [capability] for this today. The closest related functionality is [X], which [does Y].
>
> I'll leave it for a maintainer to evaluate and prioritize.

#### Question

> Thanks for reaching out. This looks like a usage question rather than a bug report.
>
> [Brief answer if obvious from the codebase, or pointer to relevant docs/help text.]
>
> A maintainer can provide more detail if needed.

#### Duplicate

> Thanks for reporting this. This appears to be related to #EXISTING_ISSUE [and/or PR #EXISTING_PR]. I'll leave it to a maintainer to confirm and link them.
