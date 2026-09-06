# Tend follow-ups

Deferred work and unimplemented options. Each entry should justify the cost
of building it if revisited.

## Cut tend over to harness = "codex" (post-release)

The Codex harness landed but tend itself still runs on Claude. The cutover
needs the release sequence:

1. Land the harness support PR on `main`.
2. Cut a release so the new tag (with `codex/action.yaml`) is what the
   version-pinned action ref resolves to.
3. Edit `.config/tend.yaml`: add `harness: codex` (and optionally
   `effort: medium`). Set `model: gpt-5.5` explicitly or let the
   default win.
4. Set `OPENAI_API_KEY` secret on `max-sixty/tend`.
   Drop `CLAUDE_CODE_OAUTH_TOKEN` from `secrets.allowed` once unused.
5. `uvx tend@latest init` to regenerate workflows. Commit both the config
   and the regenerated `tend-*.yaml` files in one commit.
6. The first nightly run after merge dogfoods the new path; watch
   `/activity` for the first review/triage and confirm token-usage parsing
   reports non-zero values.

Doing this in the same PR that ships the action would temporarily break
tend's own CI between merge and the release tag bump.

## Thread memory: deterministic prep of prior conversations

A thread's session logs share one artifact name per harness, so
`running-in-ci` finds its prior runs with a single `?name=` call, and the
agent downloads and parses them on demand. The lookup is cheap; the
cost is the agent reading raw logs (a session JSONL runs ~100 KB, ~30k
tokens) each time it opens one.

A deterministic action step would condense each matched log to its posted
text, files touched, and key reasoning (~1-2k tokens) with `jq` before the
agent sees it, stage that index on disk at a path the skill reads (or
prepend a pointer to the prompt, as the action already does for the CI
directive), and let the agent open a full JSONL only when the digest isn't
enough.

Worth building once usage shows the agent reaches for thread history often
enough that the per-log read cost is material. Until then the agent-driven
path covers the same ground, and survives no longer than the 30-day
artifact retention either way.

## Security hardening — deferred

From the old `docs/security-model.md` "what we could do but don't" — none
implemented yet:

- **Haiku pre-screening of diffs.** Cheap fast-model pass scanning for
  suspicious patterns (build-script modifications, `curl | sh`,
  base64-encoded strings, env-var reads targeting secret names). ~$0.001
  per PR. Not a security boundary (trivial to evade) but useful as a
  tripwire against unsophisticated attacks.
- **Read-only mode for fork PRs.** Restrict `allowed_tools` to
  `Glob`/`Grep`/`Read` + comment-posting MCP tools — no
  `Bash`/`Edit`/`Write`. Closes the attacker-controlled-code-execution
  gap entirely for fork PRs; trade-off is no suggested fixes on fork PRs,
  only reviews.
- **Network isolation.** Self-hosted runners with outbound traffic
  restricted to GitHub and Anthropic API endpoints. Not viable on
  GitHub-hosted runners; significant infra overhead self-hosted.

## Security channel for `tend check` drift (PVR)

The nightly `tend check` step (`plugins/tend-ci-runner/skills/nightly/`)
files one normal tracking issue for any configuration drift. Some failures
are real security regressions — missing branch protection, bot escalated to
`admin`, a deploy token (e.g. `CLOUDFLARE_API_TOKEN`) at repo level
reachable from fork-triggered runs — others are benign drift (a runtime
token needing allowlisting, a missing secret). On a *public* repo a labeled
public issue broadcasts the misconfig before it's fixed.

GitHub's native private channel is **Private Vulnerability Reporting**: a
draft repository security advisory (`POST
/repos/{owner}/{repo}/security-advisories` or the `/reports` intake) is
maintainer-private — no CVE/GHSA entry, no Dependabot alerts until
published. Deferred because:

- **Semantic misfit.** Advisories model a vulnerability in the *shipped
  package* (ecosystem, version ranges, CVSS), not a misconfiguration of
  tend's deployment. An accidental "Publish" creates a bogus GHSA.
- **Automation ergonomics.** The nightly loop needs idempotent
  find-one / refresh-footer / close-when-green — `gh issue list --search`
  gives that; an advisory's draft→triage→publish lifecycle doesn't.
- **Permission.** A maintainer draft advisory needs repo `admin`; the bot
  has `write`. Whether the `/reports` intake works with the bot's PAT (and
  whether PVR is enabled) is unverified.

If pursued: keep the tracking issue for operational drift, additionally
open a draft advisory for security-classified failures. Needs (a) the
discrimination rule (fix narrows a credential's scope → security; fix
updates config to reflect intent → drift), (b) `install-tend` enabling PVR
at setup, (c) confirming the bot token can hit the reports endpoint.

## Re-run the work a rate-limit trip refused

The `tend-rate-limit` issue lists the runs the spike limit refused, and
closing it approves the volume — but nothing re-runs them. `tend-review`
fires only on `pull_request_target`, so a refused review stays missing until
someone pushes to the PR again. Today the recovery is one
`gh run rerun <id> --failed` per row.

The shape: a generated workflow on `issues: closed`, filtered to the label
and to a closer who isn't the bot (the same check the preflight makes),
re-running rows from the last 24 hours whose run is still in `failure` —
which makes it idempotent, and stops an old row resurrecting itself. It
needs `actions: write` and, being generated, a template plus config
plumbing and generator tests. A re-run re-executes the preflight, which now
sees the approval and passes, so nothing loops.

**Blocked on** confirming tend's re-runs work correctly in the first place.
Building an automatic re-runner over a broken re-run path would bury that
bug under a second mechanism: the symptom moves from "my review never came
back" to "the recovery workflow ran and my review still never came back".

The same gap covers `tend-outage`; #816 tracks it from that side, and the
`review-runs` skill's drain recipe reads the same table format.

## Decide whether the built-in `/code-review` replaces the vendored port

`plugins/tend-ci-runner/skills/code-review/` is a copy of Claude Code's
built-in `/code-review`, taken from a 2.1.220 binary. The two share their
method almost verbatim — the same ten angles, the same three-verdict verify
with "PLAUSIBLE by default", the same sweep — so the copy buys nothing on
method and costs the usual: it drifts silently, and nothing re-checks it
against the binary.

That drift is no longer hypothetical. `claude/action.yaml` now pins 2.1.226,
which restructured the built-in without touching those texts, so the copy is
already a version behind the binary CI runs — and the thing that changed is
exactly what decides this question.

Reaching the built-in is possible. It carries `disable-model-invocation`,
which the `Skill` tool waives for a turn whose own user message names the
command, so a prompt naming `/code-review` in prose rather than opening with a
slash unlocks it for the run. That was built and reverted, because reading the
built-in out of the 2.1.226 binary says it isn't worth reaching:

- It is a model × effort matrix rather than one prompt, and `claude-opus-5` at
  both `medium` and `high` — where tend lands, running `model: opus` with no
  effort set — renders a minimal cell: one careful diff pass, no angles, no
  verify, no sweep. That cell is marked as externally measured, so it is a
  deliberate upstream choice rather than an oversight. The port's core-logic
  band is broader than it, and broader than the `default` row's 3+5 angles.
- It reports through `ReportFindings`, which `claude/action.yaml` does not
  allowlist.
- The Codex harness has no built-in at all, so the port stays the only
  implementation there regardless.

Revisit when one of those moves: a pinned version whose Opus row is
angle-based again, `ReportFindings` in the allowlist, or a Codex equivalent.
Cutting over then costs a prompt reshape plus a per-harness branch in the
`review` skill's step 5, which only pays once the built-in is the better pass.

## Worker: Phase 2 LLM summary of `/activity`

A consumer (scheduled job or the Worker calling Claude) reads `/activity`
and writes a short prose summary of what tend's been up to; the summary
lives in KV and is what the site renders. If the summary wants a longer
span than the last week (beyond GitHub's ~90-day events window or one
Search page), a KV/D1 accumulator that appends activity as it arrives
earns its keep — until then, demand-fetch is cheap enough.
