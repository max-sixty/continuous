# Tend follow-ups

Deferred work and unimplemented options. Each entry should justify the cost
of building it if revisited.

## Finish moving the operational secrets into the `tend` environment

The gate closes on a repo only when the repo-level copy is gone, since a job
naming an environment still reads repo-level secrets. Every adopter now has
the environment, admitting only `main`, with `TEND_BOT_TOKEN` in it, and every
generated workflow names it. What remains:

1. **The model credential, every repo.** `CLAUDE_CODE_OAUTH_TOKEN` can't be
   read back and isn't stored anywhere locally, so it has to be pasted or
   re-minted with `claude setup-token`:
   `gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo <repo> --env tend`, then
   `gh secret delete CLAUDE_CODE_OAUTH_TOKEN --repo <repo>`. Only the
   generated workflows read it, and all of them name the environment, so
   nothing else breaks.
2. **`PRQL/prql` keeps a repo-level `TEND_BOT_TOKEN`,** because
   hand-maintained workflows read it outside the generated set.

   Jobs running at `refs/heads/main` are admitted by the policy as it stands,
   so adding `environment: {name: tend, deployment: false}` is the whole
   change: `update-rust-toolchain` (`schedule`-only, and already `if`-gated to
   it) and backport. A `pull_request_target` run reports
   `GITHUB_REF=refs/heads/main` and is admitted — which also means the
   environment is not what gates that job.

   The release jobs run on `release`, which is bot-steerable: creating a
   release against an existing tag takes no tag operation, so the tag ruleset
   does not stop it and a tag entry would not gate it. One of:

   - A required reviewer on a release environment, costing an approval on
     every release.
   - Moving the release workflow to a tag push. A tag push is not
     bot-steerable and the all-tags ruleset gates it, so the release jobs take
     a second environment whose policy admits the tag pattern — the shape
     `check_credential_environments` credits under that ruleset — holding a
     second copy of `TEND_BOT_TOKEN`, so no new credential. The tag entry does
     not go on the `tend` policy, whose shape `check_environment` pins to
     exactly the protected branches and whose `--fix` deletes anything else.
     This is what `max-sixty/worktrunk` runs.
   - A second credential, left at repo level and allowlisted. This narrows the
     `homebrew-prql` half alone: `publish-winget` runs `winget-releaser`,
     which supports only a *classic* PAT, and the `public_repo` scope it needs
     is not per-repo — it carries write to every public repository the account
     can reach, `PRQL/prql` included. It also needs `push-web-branch` to drop
     to `GITHUB_TOKEN`, so check whether anything runs on the `web` branch — a
     `GITHUB_TOKEN` push fires no workflow.

   Until one lands, `repo-secret-allowlist` fails, correctly.

`numbagg/numbagg`, `max-sixty/cargo-affected`, and `max-sixty/worktrunk` are
done bar the model credential.

The check also sweeps every *other* credential-holding environment — one
that stores a secret, or that a job requesting `id-token: write` deploys
to — so a repo with a pre-existing publish environment may fail here until
that environment is gated: a required reviewer that is not the bot, or a
policy naming only verified branches — with tag entries needing the
admin-only all-tags ruleset the install recipe's §3 creates. A trusted
publisher stores no secret, so this is where a repo that publishes to PyPI
or npm shows up. That failure is the check doing its job; gate the
environment rather than allowlisting around it.

`tend-agent/tend-integration` migrates itself: its `integration-secrets`
reseed writes both secrets into the environment and deletes the repo-level
copies once the fixture's workflows name it. The fixture regenerates with the
published `tend`, so this completes on the first weekly after 0.1.13.

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

## Auth: GitHub App alternatives to PAT

Both alternatives replace the classic PAT (long-lived, leak-permanent) with
a GitHub App installation token (~1 h lifetime, repo-scoped). This is the
single highest-impact change for token-leak risk.

### Model A: token-minting service

Adopter installs our GitHub App; `tend init` generates the same workflow
files. The only auth change is an OIDC call to our service that mints a
scoped installation token per workflow run. Workflows still live and run
in the adopter's repo.

```yaml
- uses: max-sixty/tend/auth@X.Y.Z   # OIDC → our service → scoped token
  id: auth
- uses: actions/checkout@v6
  with:
    token: ${{ steps.auth.outputs.token }}
- uses: max-sixty/tend/claude@X.Y.Z
  with:
    github_token: ${{ steps.auth.outputs.token }}
    claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Trust model: standard GitHub App — adopters trust the App by installing it,
like installing Codecov or Renovate. We hold the App private key; adopters
hold their own Claude OAuth token. A workflow-run OIDC token
(`id-token: write`) proves the caller's repo identity to our service.

Could be extended to push workflow updates via PR, but that requires a
webhook handler to detect config changes.

### Model B: full webhook handler

Adopter installs our GitHub App, adds `.config/tend.yaml`, done — no
workflow files. GitHub sends raw events to our service; we run the logic
(engagement verification, concurrency, dispatch) and execute Claude on our
infrastructure (or dispatch back to the adopter's runners).

Most cohesive UX, and partially addresses the fork-PR gap — we receive
inline review-comment webhooks regardless of fork status.

Trade-offs: a compromise of our infra exposes write access to every
adopter's repo *and* their code. Anthropic token has three options:

- Adopter hands it to us; we hold it. If our service is compromised, the
  attacker gets every adopter's Claude token.
- We provide Claude access and bill the adopter. Simpler for them; we take
  on billing and usage management.
- `workflow_dispatch` back to their runners. Token stays in their secrets;
  adds latency and complexity.

## Auth: triage + fork privilege model

Currently only `write + branch protection` exists. The planned `mode` field
in `.config/tend.yaml` would select between two models:

| | **Triage + fork** | **Write + branch protection** (current) |
|---|---|---|
| Bot collaborator level | Triage | Write |
| Bot pushes code to | Own fork | Target repo branches |
| Creates PRs | From fork | Same-repo |
| Approvals count for required reviews | No | Yes |
| Branch protection required | **No** | **Yes** — primary security boundary |
| Leaked PAT blast radius | Comments/reviews; fork write only | Full write to target repo |
| Setup complexity | Low | Medium |

`Triage + fork` would be the recommended default. The bot pushes to its
own fork and creates cross-fork PRs:

```bash
git remote add fork https://x-access-token:${TEND_BOT_TOKEN}@github.com/${BOT_NAME}/${REPO}.git
git push fork fix/ci-123
gh pr create --repo ${TARGET_REPO} --head ${BOT_NAME}:fix/ci-123
```

Limitations: triage-level approvals don't satisfy required-review policies,
and triage can't push to human PR branches — the bot posts review
suggestions instead.

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
- **Workflow dispatch isolation.** Split each workflow into an analysis
  job (`GITHUB_TOKEN` only, reads the diff, produces a plan) and a push
  job (bot token, separate workflow triggered by `workflow_run`). The bot
  token never enters a job that touches attacker-controlled code.
  Significant complexity — every workflow becomes two with artifact
  passing between them.

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

## Worker: Phase 2 LLM summary of `/activity`

A consumer (scheduled job or the Worker calling Claude) reads `/activity`
and writes a short prose summary of what tend's been up to; the summary
lives in KV and is what the site renders. If the summary wants a longer
span than the last week (beyond GitHub's ~90-day events window or one
Search page), a KV/D1 accumulator that appends activity as it arrives
earns its keep — until then, demand-fetch is cheap enough.
