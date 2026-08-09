---
name: code-review
description: Structured second pass over a diff — correctness and cleanup angles, a verify pass, ranked findings returned to the caller. Use as the independent pass inside a PR review, or when asked for a thorough diff review.
argument-hint: "[target]"
metadata:
  internal: true
---

# Code review

A structured pass over a diff that returns ranked findings. Ported from Claude Code's built-in `/code-review`. The Codex harness has no built-in equivalent, and on Claude the built-in carries `disable-model-invocation`, which no tend prompt lifts. This copy is tend-owned: reachable from the model on either harness.

**Return findings; don't act on them.** No review, comment, commit, or artifact from this skill — the caller folds the findings into its own single review.

## Phase 0 — Gather the diff

If a target was passed (PR number, branch, ref range, path, or a free-form scope instruction), review that. Otherwise run `git diff @{upstream}...HEAD` (falling back to `git diff main...HEAD` or `git diff HEAD~1`); if there are uncommitted changes, or the range diff is empty, also run `git diff HEAD`. That unified diff is the review scope.

Read the CLAUDE.md files that govern the changed code: the repo-root one, plus any `CLAUDE.md`/`CLAUDE.local.md` in a directory that is an ancestor of a changed file (a directory's CLAUDE.md applies only to files at or below it).

## Phase 1 — Find candidates

Work through the angles below. Each surfaces candidates with `file`, `line`, a one-line `summary`, and a concrete `failure_scenario`.

Scale the fan-out to the change, the same way the caller scales its own depth:

- **Peripheral or mechanical** (docs, config, dependency bumps, test-only): angles A–C plus the cleanup and conventions angles, in one pass, in this context. Up to 4 candidates each.
- **Core logic**: every angle, up to 6 candidates each. Work through them yourself, in sequence, in one pass — that's the default. Fan out one subagent per angle (`Task`/`Agent`) only where this session permits subagent use; the Claude harness allowlists the tool but its system prompt limits it to user-requested calls. Either way, don't skip angles for lack of fan-out.

Don't let one angle's conclusions suppress another's: if two angles flag the same line for different reasons, record both. Pass every candidate with a nameable failure scenario through to Phase 2 — finders that silently drop half-believed candidates bypass the verify step and are the dominant cause of misses.

### Angle A — line-by-line diff scan

Read every hunk in the diff, line by line. Then read the enclosing function for each hunk — bugs in unchanged lines of a touched function are in scope (the PR re-exposes or fails to fix them). For every line ask: what input, state, timing, or platform makes this line wrong? Look for inverted/wrong conditions, off-by-one, null/undefined deref, missing `await`, falsy-zero checks, wrong-variable copy-paste, error swallowed in catch, unescaped regex metachars.

### Angle B — removed-behavior auditor

For every line the diff deletes or replaces, name the invariant or behavior it enforced, then search the new code for where that invariant is re-established. If you can't find it, that's a candidate: a removed guard, a dropped error path, a narrowed validation, a deleted test that was covering a real case.

### Angle C — cross-file tracer

For each function the diff changes, find its callers (grep for the symbol) and check whether the change breaks any call site: a new precondition, a changed return shape, a new exception, a timing/ordering dependency. Also check callees: does a parallel change in the same PR make a call unsafe?

### Angle D — language-pitfall specialist

Scan for the classic pitfalls of the diff's language or framework — for example: JS falsy-zero, `==` coercion, closure-captured loop var; Python mutable default args, late-binding closures; Go nil-map write, range-var capture; SQL injection; timezone/DST drift; float equality. Flag any instance the diff introduces.

### Angle E — wrapper/proxy correctness

When the PR adds or modifies a type that wraps another (cache, proxy, decorator, adapter): check that every method routes to the wrapped instance and not back through a registry/session/global — e.g. a caching provider holding a `delegate` field that resolves IDs via `session.get(...)` instead of `delegate.get(...)` will re-enter the cache or recurse. Also check that the wrapper forwards all the methods the callers actually use.

### Reuse

The angles above hunt for bugs; this one and the next three hunt for cleanup in the changed code. Flag new code that re-implements something the codebase already has — grep shared/utility modules and files adjacent to the change, and name the existing helper to call instead.

### Simplification

Flag unnecessary complexity the diff adds: redundant or derivable state, copy-paste with slight variation, deep nesting, dead code left behind. Name the simpler form that does the same job.

### Efficiency

Flag wasted work the diff introduces: redundant computation or repeated I/O, independent operations run sequentially, blocking work added to startup or hot paths. Also flag long-lived objects built from closures or captured environments — they keep the entire enclosing scope alive for the object's lifetime (a memory leak when that scope holds large values); prefer a class/struct that copies only the fields it needs. Name the cheaper alternative.

### Altitude

Check that each change is implemented at the right depth, not as a fragile bandaid. Special cases layered on shared infrastructure are a sign the fix isn't deep enough — prefer generalizing the underlying mechanism over adding special cases.

### Conventions (CLAUDE.md)

Check the diff against the CLAUDE.md files read in Phase 0. Only flag a violation when you can quote the exact rule and the exact line that breaks it — no style preferences, no "spirit of the doc" inferences. Name the CLAUDE.md path and quote the rule so the report can cite it. If no CLAUDE.md applies, return nothing for this angle.

Cleanup, altitude, and conventions candidates use the same `file`/`line`/`summary` shape; in `failure_scenario`, state the concrete cost (what is duplicated, wasted, harder to maintain, or which CLAUDE.md rule is broken) instead of a crash. Correctness bugs always outrank cleanup, altitude, and conventions findings when the output cap forces a cut.

## Phase 2 — Dedup and verify

Dedup candidates that point at the same line and mechanism, keeping the one with the most concrete failure scenario. Then verify each remaining candidate against the diff and the relevant files — yourself, in this context, by default; one subagent verifier per candidate only where this session permits subagent use. Each candidate gets exactly one of:

- **CONFIRMED** — you can name the inputs/state that trigger it and the wrong output or crash. Quote the line.
- **PLAUSIBLE** — mechanism is real, trigger is uncertain (timing, env, config). State what would confirm it.
- **REFUTED** — factually wrong (the code doesn't say that) or guarded elsewhere. Quote the line that proves it.

**PLAUSIBLE by default.** Don't refute a candidate for being "speculative" or "depends on runtime state" when the state is realistic: concurrency races, nil/undefined on a rare-but-reachable path (error handler, cold cache, missing optional field), falsy-zero treated as missing, off-by-one on a boundary the code doesn't exclude, retry storms or partial failures, a regex/allowlist that lost an anchor. Those are PLAUSIBLE.

**REFUTED** only when constructible from the code: factually wrong (quote the actual line); provably impossible (type, constant, or invariant — show it); already handled in this diff (cite the guard); or pure style with no observable effect.

Keep CONFIRMED and PLAUSIBLE; drop REFUTED.

## Phase 3 — Sweep for gaps

On a core-logic change, take one more pass as a fresh reviewer holding the verified list. Re-read the diff and the enclosing functions looking **only** for defects not already listed — the job is gaps, not re-confirmation. Focus on what a first pass tends to miss: moved or extracted code that dropped a guard or anchor; second-tier footguns (dataclass default evaluated once, `hash()` non-determinism, lock-scope shrink, predicate methods with side effects); setup/teardown asymmetry in tests; config defaults flipped. If nothing new, return nothing — don't pad.

## Output

Return the findings to the caller as a list of at most 10 (at most 4 for a peripheral change), ranked most-severe first, each with `file`, `line`, `summary`, `failure_scenario`, and its verdict. If nothing survives verification, say so in one line. Don't call the `ReportFindings` tool and don't publish an artifact — the caller owns the output.

Tell the caller which mode ran (fanned-out angles with a subagent verify, or a single inline pass) so it can weigh the findings — context for the caller, not content for the review it posts.
