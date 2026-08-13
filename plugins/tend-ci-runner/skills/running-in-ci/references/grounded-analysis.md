# Grounded analysis

Depth behind `running-in-ci`'s **Grounded Analysis** section: how to establish
that a claim is true before a permanent public comment carries it.

- [Source evidence for user-facing claims](#source-evidence-for-user-facing-claims)
- [Verifying external-tool behavior](#verifying-external-tool-behavior)
- [Recurring hallucination shapes](#recurring-hallucination-shapes)
- [Transient incidents vs. durable bugs](#transient-incidents-vs-durable-bugs)
- [Who to ask when you can't do it yourself](#who-to-ask-when-you-cant-do-it-yourself)

## Source evidence for user-facing claims

Before posting any specific claim — a configuration snippet, command syntax,
variable name, or API behavior — find the **source text** that confirms it:
documentation, help output, test expectations, or the code implementing the
public interface. Internal implementation code shows what exists internally,
not how it's exposed; read the docs or user-facing layer too.

<example>
<bad reason="Read Rust code showing a 'target' variable and invented $WT_TARGET">

Bad: Saw `extra_vars.push(("target", target_branch))` in Rust source → posted a hook example using `$WT_TARGET` (an environment variable that doesn't exist — hooks use `{{ target }}` Jinja templates).

</bad>
<good reason="Verified syntax against user-facing documentation before posting">

Good: Saw `("target", target_branch)` in Rust source → read `docs/hook.md` → confirmed hooks use `{{ target }}` syntax → posted correct example.

</good>
</example>

For **behavioral claims** — "X happens when you run Y" — reading code is not
enough. Conditional branches, early returns, and error paths are easy to miss
when tracing mentally. Find a test that exercises that exact scenario, or run
the command. If neither is feasible, hedge explicitly: "Based on code reading,
I believe X, but I haven't verified this end-to-end."

<example>
<bad reason="Traced one code path but missed a guard clause in a called function">

Bad: Read `CommandEnv::for_action("commit", config)` → saw it constructs an env → concluded `wt step commit` works in a detached worktree. Missed that `for_action()` calls `require_current_branch()`, which errors on detached HEAD.

</bad>
<good reason="Built and tested the actual behavior before claiming">

Good: Read `for_action()` → noticed it calls `require_current_branch()` → uncertain whether detached HEAD hits that path → ran `cargo build && wt step commit` in a detached worktree → confirmed the error → posted accurate answer.

</good>
</example>

Link to user-facing documentation where a project has it — finding the link
forces verifying the claim. Where no source evidence turns up, say so ("I'm not
sure of the exact syntax"). An honest gap is fixable; a confident hallucination
gets copy-pasted.

**Rewriting is authoring.** Cross-posting, summarizing, or paraphrasing carries
the same bar for anything *added*: a config section header inferred from a
command name (`[step]` from `wt step`, where the real section is `[aliases]`)
is a fresh claim, not a copy.

## Verifying external-tool behavior

When a claim turns on how an external CLI, API, or system behaves, verify by
running the code. Two paths, in order:

1. **Run the tool.** If it's installable here, install it and invoke the
   specific command or flag. Link the output in your reply.
2. **Read the source.** Tend can clone any public repo. `gh repo clone
   <owner>/<repo>`, then grep for the flag or behavior. Source doesn't lag
   itself, and a flag the parser doesn't define doesn't exist.

If both fail (GUI-only tool, private repo, environment-specific behavior), cite
what you found and name the gap — then see **Who to ask** below.

<example>
<bad reason="Trusted upstream docs for a fast-moving external CLI and shipped a broken recipe">

Bad: Review asked whether `cmux list-workspaces` had structured output. Read a mintlify page describing `--json` → rewrote the recipe to `cmux list-workspaces --json | jq ...` → committed. The installed cmux had no `--json` flag; every reader hit a broken recipe.

</bad>
<good reason="Cloned the upstream source and verified the flag before shipping">

Good: Same question. Cloned cmux's source repo → grepped the CLI parser for `list-workspaces` → saw no `--json` flag defined → replied with the source link and proposed an alternative that matched the actual CLI surface.

</good>
</example>

**Path 1 runs against the live repo.** Verifying a skill's own recipe is the
common case, and those recipes write: `gh issue close`, `gh pr comment`, `git
push`. Never extract a block programmatically to run it — not by position
(`awk` on the Nth fence, `sed` on a line range) and not by anchor: both hand
you a block you haven't read, and the ordinal moves with every edit, so what
runs isn't even the block you meant to test. Read the file, then run the
commands directly. Run the read half and stop before the pipe into the write:

```bash
gh issue list --state open --author '@me' --search '"..." in:title' --json number --jq '.[].number'
# ...and read that, rather than piping it into `xargs gh issue close`.
```

If the write is the part in question, point it at a scratch object you own. A
wrong write is only partly recoverable: reopening an issue leaves the close in
its timeline, and a deleted comment has already fired its `issue_comment`
event, so any workflow it triggered ran and is still in the run list.

## Recurring hallucination shapes

**Links must be fetched, not guessed.** Before pasting any URL, run `curl -sI
<url> | head -1` and confirm `200`. Docs-site slugs are treacherous —
`escaping.html`, `quoting.html`, and `quote-strings.html` are all plausible;
only one (or none) exists.

**`--jq` projections must keep the ID when downstream URLs cite individual
items.** Composing `actions/runs/<id>`, `#issuecomment-<id>`, or `pull/<n>`
needs the ID in the projection (`databaseId` for runs, `id` for comments,
`number` for PRs/issues). A projection that kept only timestamps or titles
leaves the bot fabricating the missing ID, and the link 404s. Re-query instead.

**`gh` list commands truncate silently — pass `--limit` whenever the result set
is the answer.** `gh issue list`, `gh pr list`, and `gh search` return 30 items
by default; `gh run list` returns 20, and nothing in the output says it
truncated. A dedup scan then misses the existing issue past the cap and opens a
duplicate; a survey reports complete coverage of the rows it happened to see. A
count that reads exactly the default across repeated measurements is the
signature. Client-side filtering inside `--jq` is the worst variant: the filter
hides the truncation, so a capped result reads as a legitimately short one.

**"Likely" is a stop-sign.** A hedge in a user-facing claim — "likely works",
"probably parses as", "I think" — means it rests on an unverified guess. Verify
and replace the hedge with the answer, or hedge explicitly ("I haven't tested
this — would appreciate if you can confirm"). The shape is the tell, not the
exact words: an unverified guess dressed as confident analysis erodes trust
fastest.

**Never ship literal placeholders.** `<PLACEHOLDER>`, `PR #PLACEHOLDER`,
`<SHA>`, `TBD`, `XXX`, `<TODO(fill)>` in an issue body, PR body, or comment are
corruption — a deferred substitution that never ran, rendered permanently.
Sequence the work so a referenced artifact exists before the referencing body
is composed: create the PR → read its number → compose the issue with the
number filled in → file it. Where the cross-reference can't be resolved before
posting, omit it or rephrase ("a follow-up PR will…"). Before any `gh issue
create`, `gh pr create`, or `gh ... comment --body-file`, grep the body for
those strings and refuse to post on a match. A session that times out
mid-sequence leaves an unsubstituted placeholder visible forever —
pre-substitute, don't post-substitute.

## Transient incidents vs. durable bugs

Intermittent or inconsistent behavior — the same query returning different
results within seconds, an API returning empty when records demonstrably exist,
a CLI flag working sometimes — points at an active upstream incident more
strongly than at a CLI or skill bug. Reproducing the flake confirms the symptom,
not the cause, and a code workaround committed during an incident outlives it.
Check upstream status before designing one. For GitHub-side symptoms:

```bash
# Fetch first, parse second. The endpoint sits behind an edge that sometimes
# answers a CI runner with an HTML challenge page instead of JSON; piping that
# straight into jq gives a parse error on stderr and an empty stdout, which
# reads exactly like "no open incidents". `-f` catches a challenge served as a
# non-200 and the `jq -e` probe catches one served as 200; capturing the body
# means the status is curl's or jq's, not a pipeline's (a bare
# `curl … | jq … || …` exits 0 on the challenge page).
if ! INCIDENTS=$(curl -fsS 'https://www.githubstatus.com/api/v2/incidents/unresolved.json') \
   || ! echo "$INCIDENTS" | jq -e . >/dev/null 2>&1; then
  echo 'STATUS PROBE FAILED — upstream state unknown, not clear'
else
  echo "$INCIDENTS" \
    | jq '.incidents[] | {created_at, name, impact, components: [.components[].name]}'
fi
```

**A failed probe is `unknown`, never `clear`.** Empty output from a successful
query means no open incident; a probe that errored means you didn't check. Both
resolve the same way — record the symptom in the evidence log and skip the
workaround PR — so an unreachable status endpoint is not a reason to file one.

If the response is non-empty and the components and timing match the symptom,
record it in the run's evidence log and exit without a PR. Sibling matrix legs
hitting different surface symptoms of one incident otherwise each open their own
near-duplicate workaround PR — title and file dedup don't catch them, because
each leg picks a different command to mitigate.

<example>
<bad reason="Reproduced an API flake during an active incident, opened code workarounds without checking upstream status">

Bad: `gh issue list` returns `[]` intermittently for queries whose matching issues clearly exist. Bot opens a PR adding a retry loop. A sibling matrix leg sees the same shape on `gh run list` and opens its own workaround PR swapping to client-side filtering. Both are workarounds for an active upstream search-degradation incident; both get closed once the incident link surfaces.

</bad>
<good reason="Checked status.github.com first, treated the symptom as transient">

Good: Same flake → `curl /api/v2/incidents/unresolved.json` returns an active "GitHub search is degraded" incident touching Issues + Pull Requests → record the symptom in the evidence log, skip the PR, let the incident resolve.

</good>
</example>

## Who to ask when you can't do it yourself

Some checks need hardware or an environment CI doesn't have (Windows, a GPU, a
physical terminal). Escalate in this order and stop at the first rung that works:

1. **Do it yourself.** Exhaust what's reachable from CI — install the tool,
   clone and read the source, stand up the missing surface in a container.
2. **Make it doable yourself.** Add the capability to *your own* repo so no
   future run needs a favor — a Windows CI job that exercises the path, rather
   than asking a person to run it once by hand.
3. **Ask a contributor of your own repo**, and only for something that follows
   from what they're already doing (a PR author testing their own change).
4. **Escalate to your own repo's maintainer** that you're blocked.

Never route the ask *outward* — least of all to the maintainer of another repo
who is reviewing or merging your change as a favor. Closing an upstream PR with
"if you can confirm on a real Windows terminal I'd appreciate it" hands them
work; state the gap honestly ("verified by source inspection, not on hardware")
and take rung 2 back home instead.
