# Acting in other repos

Depth behind `running-in-ci`'s **Other Repos** section.

- [Filing issues](#filing-issues)
- [Contributing on invitation](#contributing-on-invitation)
- [When a scope rule blocks the right action](#when-a-scope-rule-blocks-the-right-action)

## Filing issues

Default: file an issue in the current repo asking for permission to file in the target. On maintainer approval, file in the target.

The adopter's `running-tend` overlay may grant a standing exception for **agent-equipped** targets — repos that run their own coding agent. Signals:

- `.github/workflows/tend-*.yaml` present (the target uses tend).
- A workflow invokes `anthropics/claude-code-action` or another coding-agent action.
- Recent issues or PRs authored by a bot account, with no human pushback in the thread.

Two or three convergent signals are enough; borderline cases revert to the default. Without an explicit opt-in in `running-tend`, the default also applies.

When asking permission (the default path), close with a short offer so the user can record a preference for future asks. The offer should let them pick either outcome: have the bot file without asking next time, or keep approving each one but stop seeing the offer. Phrase it to fit the thread.

Either reply gets codified in the consumer repo's `running-tend` overlay per `skill-pr-workflow.md` — opt-in adds the target (or "all agent-equipped targets") to the exceptions list; suppress adds a one-line rule telling the bot to skip the offer for future asks.

Whether filed direct or post-approval, the issue body includes:

- Problem statement: what fires, where, under what conditions.
- Evidence: run links; cost/duration if relevant.
- Proposed fix with code snippets a maintainer would otherwise re-derive.

## Contributing on invitation

SKILL.md's **Restrictions → Scope** bars *unsolicited* PRs/comments in other repos. It does not bar an *invited* one. When BOTH hold, the bot may open a PR or comment on an existing thread in the target repo:

- **Explicit invitation** — a maintainer of the target repo asked for the contribution in-thread (e.g. "do you want to open the PR?"), or the target's published contributing policy welcomes outside contributions of this kind. Inferred welcome (agent signals, an open "help wanted" label without a direct ask) is not enough — that reverts to the default.
- **Serves the home repo** — the contribution advances the repo the bot maintains, most often upstreaming a fix for a dependency bug the bot is currently working around locally, so the workaround can later be dropped.

Keep it to the invited scope: the specific PR or comment asked for, attributed to the bot account, with the same evidence bar as any other output (repro, traced mechanism, verified fix). Don't expand into unrelated upstream work. If the invitation is ambiguous, treat it as absent and surface it to the home maintainer per **When a scope rule blocks the right action** below.

## When a scope rule blocks the right action

When that Scope restriction is the only thing between you and the correct move (e.g. the right step is to add evidence to an existing upstream thread, which the rule bars), don't silently substitute a workaround and report success — that hides the wall.

Surface the blocker on the triggering thread and offer the maintainer both:

1. **Take the upstream action on approval** — file a fresh issue, or note evidence on the existing thread.
2. **Relax the rule going forward** — via the consuming repo's `running-tend` overlay.

Record their choice per `skill-pr-workflow.md`.
