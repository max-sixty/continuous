# tend-website Worker

Cloudflare Worker that serves the two data streams the tend marketing site
renders. Reads `data/consumers.json` from this repo, fans out to GitHub, and
serves CORS-enabled JSON.

| Stream | Endpoint | Freshness budget | Fallback budget |
| --- | --- | --- | --- |
| Currently tending | `/currently-tending` (also `/`) | 30 s | 5 s |
| Activity | `/activity` | 5 min | 30 s |

Base URL: `https://api.tend-src.com`. `Access-Control-Allow-Origin: *`
(public read-only data).

## Why a Worker, not browser-direct

Unauthenticated GitHub REST is 60 req/hour/IP and the Search API is 10
req/min/IP — both shared across everyone behind a NAT. A single
currently-tending poll fans out one `actions/runs` call per consumer repo every
30 s; `/activity` issues one Search query per bucket. One browser tab
would exhaust those quotas in minutes. The Worker holds an authenticated token
(5,000 req/hour, 30 Search req/min) and caches each route at the colo, so
origin load is bounded by the freshness budget, not by viewer count. Static
nightly JSON would cover `/activity` but can't meet currently-tending's
sub-minute freshness budget, so both live on the one Worker.

## Endpoint shapes

### `/currently-tending`

```jsonc
{
  "generated_at": "2026-05-10T17:30:00Z",
  "currently_tending": [
    {
      "repo": "max-sixty/tend",
      "workflow": "tend-review",
      "started_at": "2026-05-10T17:29:14Z",
      "run_url": "https://github.com/max-sixty/tend/actions/runs/12345"
    }
  ]
}
```

Source: `GET /repos/{owner}/{repo}/actions/runs?status=in_progress` per
consumer, filtered to workflows whose `name` starts with `tend-`.

**UI fallback:** when `currently_tending` is empty or the Worker request
fails, the UI falls back to showing "last tended N min ago" from the most
recent item in `/activity` — the indicator never breaks the page. This
fallback lives in the rendering layer (`site/src/components/CurrentlyTending.astro`),
not the data layer.

### `/activity`

Recent things tend has done, in primitive buckets — one Search query per
bucket (`sort=updated`): the page yields both the `recent` items and the
lifetime `count` (`total_count`); `count_this_week` is counted off the page,
so it saturates around one page (~100) per bucket — fine for a headline
number.

Each query covers every bot at once. Repeating a qualifier ORs it
(`author:a author:b` matches either), so the consumer list folds into a single
query and a refresh costs **4 Search requests regardless of how many consumers
there are**. That bound is the point: the cap is 30 req/min, so the earlier
one-query-per-bot shape (4·N) crossed it at 8 consumers. Nothing about that
failure is loud — `searchIssues` throws on a non-OK status and the throw sinks
the whole refresh, the `activity` fallback TTL is 30 s so it re-attempts about
twice a minute and never drains, and the site hides a section whose fetch
returned nothing rather than erroring. The visible result would have been a
blank stat strip. The KV sharing tier (see [Caching](#caching)) still bounds
how often a refresh fires, but it is no longer what keeps the burst under the
cap.

The request count is now constant, but query *length* still grows with the
consumer list — `comments` is the longest at three qualifiers per bot, and
measured ~570 characters at 8 consumers. That is the ceiling this shape has
left, replacing the old ~7-bot one; it fails the same silent way, so if the
consumer list grows several times over, check a `comments` query against live
Search before assuming it still returns 200. The 20-consumer test asserts the
request count against a mocked `fetch` and says nothing about that.

Two consequences of combining, both benign at present. A `count` no longer
double-counts an item two bots both touched (possible for `reviews` and
`comments`, not for `author:`-keyed buckets, and unobserved — each bot works
in its own repo). And the matching bot is no longer named in the result, so
the deep-link follow-up below matches the whole consumer list instead of one
login; it reads the same single page either way, so it costs no extra
requests.

```jsonc
{
  "generated_at": "2026-05-10T17:30:00Z",
  "prs":      { "count": 485,  "count_this_week": 6,  "recent": [ /* RecentItem */ ] },
  "issues":   { "count": 82,   "count_this_week": 2,  "recent": [ ... ] },
  "reviews":  { "count": 1102, "count_this_week": 28, "recent": [ ... ] },
  "comments": { "count": 206,  "count_this_week": 4,  "recent": [ ... ] }
}
```

`RecentItem` = `{ repo, title, url, at }`. `at` is the parent issue/PR's
`updated_at`, and `title` is the parent's title — Search returns the item, not
the comment or review body. Newest-first, ≤10 per bucket.

For `reviews` and `comments`, `url` deep-links to the bot's latest inline
review comment (`…#discussion_r<id>`) or conversation comment
(`…#issuecomment-<id>`), so clicking lands on tend's actual action rather
than the top of the thread. The Worker does one extra GitHub REST call per
recent item to resolve the anchor (`/repos/{repo}/pulls/{n}/comments` for
reviews — the inline-comment endpoint, since tend's reviews are
`COMMENTED` with empty bodies and the review anchor scrolls nowhere;
`/repos/{repo}/issues/{n}/comments` for comments) and falls back to the
parent URL if the follow-up fails. For `prs` and `issues`, `url` is the
parent issue/PR — that is what the bot created.

`<bots>` below stands for the qualifier repeated once per consumer bot. Negated
qualifiers AND rather than OR, so `comments` excludes anything authored or
reviewed by *any* bot, not just the one that commented.

| bucket | Search query | "some bot …" |
| --- | --- | --- |
| `prs` | `author:<bots> is:pr` | opened these PRs (any state) |
| `issues` | `author:<bots> is:issue` minus five bookkeeping labels (`tend-outage`, `tend-rate-limit`, `review-runs-tracking`, `review-reviewers-tracking`, `nightly-cleanup`) | opened these issues, filed against the repo — tend's own outage and tracking issues are excluded |
| `reviews` | `reviewed-by:<bots>` | reviewed these PRs (approve / request-changes / review comment) — by volume, tend's main action |
| `comments` | `commenter:<bots> -author:<bots> -reviewed-by:<bots>` | commented on these PRs/issues — excludes bots' own threads and items already in `reviews` |

> **TODO — Phase 2:** a consumer (a scheduled job, or the Worker calling Claude)
> reads `/activity` and writes a short prose summary of what tend's been up to;
> the summary lives in KV and is what the site renders. If that summary wants a
> longer span than the last week (beyond GitHub's ~90-day events window or one
> Search page), that's when a KV/D1 accumulator that appends activity as it
> arrives earns its keep — until then, demand-fetch is cheap enough.

### Multi-bot semantics

Everything is **merged across bots**: each `/activity` bucket is one Search
query covering every bot, so `count` and `count_this_week` are union counts
over all tend bots rather than per-bot sums, and its `recent` list is the
newest items across all of them; `currently_tending` is the union of all
bots' in-progress runs. Activity is *not* scoped to consumer repos — `count`
comes from Search's `total_count`, which can't be filtered post-hoc — but a tend
bot only acts in its own repo, so this is a distinction without a difference in
practice.

## Input: `data/consumers.json`

Each tend-using repo is one entry — produced by `running-tend`'s weekly
refresh.

```json
[
  {"repo": "owner/name", "bot_name": "tend-agent"},
  ...
]
```

The Worker fetches this via
`raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json` and
caches it in KV for 1 h, so a `consumers.json` commit propagates within
the hour.

## Caching

Two tiers. The **colo cache** (`caches.default`) is the hot, per-data-center
tier: every request is answered from it, never waiting on the GitHub fanout.
Past its freshness budget (30 s / 5 min) a hit also kicks off a background
refresh via `ctx.waitUntil`, so the next viewer sees fresher data. An entry
stays serveable for ten freshness budgets (5 min on `/currently-tending`,
50 min on `/activity`) before the cache drops it.

The colo cache is per-colo, so each Cloudflare data center would otherwise fan
out to GitHub on its own. `/activity` adds a **KV tier** (`activity:v1`)
behind the hot tier: a refresh publishes its rendered payload to KV, a global
store, so a refresh in one colo serves every other colo. The GitHub fanout
then fires about once per freshness budget across the whole network instead of
once per colo. With the burst itself fixed at 4 requests, that is now headroom
rather than the thing keeping it under the 30 req/min cap — it bounds how many
near-simultaneous refreshes can stack, whatever the colo count. KV survives
deploys and outlives the colo cache, so a
viewer waits on the fanout only when colo cache and KV are both empty: the
first request ever, or after an idle longer than KV's retention.
`/currently-tending` skips KV; its 30 s budget is under KV's 60 s floor, and
its fanout is cheap core REST, not the Search quota the sharing protects.

Still demand-driven: a refresh fires only on a request (foreground on a true
cold start, background otherwise), so a no-traffic day costs zero GitHub
calls. The KV coordination is best-effort, not a global lock: colos going
stale at the same instant can still race within KV's write-propagation window
(up to ~60 s) before the first write is visible, after which later arrivals
read the fresh entry and skip the fanout. A cron-driven single refresher would
make that a hard guarantee but trade away the zero-when-idle property; not
worth it at this scale.

When a refresh throws (GitHub outage) on a warm colo cache, the bumped stale
entry is kept, so an outage never overwrites good data with zeros. On a cold
colo cache, `/activity` serves a prior KV entry (stale if need be) and
revalidates in the background, so even a cold start during an outage shows the
last-known data. Only when colo cache and KV are both empty does a thrown
refresh return a 503 rather than a 200 all-zero payload, so the site renders
nothing for that section instead of fabricated zeros. The 503 is
negative-cached at the short **fallback budget** (5 s / 30 s) so the next
request retries soon.

## Topology

```
data/consumers.json on main
  └─ refreshed weekly by running-tend's `weekly` task (PR-gated)

.github/workflows/worker-deploy.yaml          on push to main worker/**
  └─ deploys worker/ to Cloudflare

Cloudflare Worker (tend-website)
  ├─ reads data/consumers.json via raw URL (KV-cached 1 h)
  ├─ /currently-tending: fans out actions/runs per repo (in-progress, tend-*)
  ├─ /activity:          one Search query per bucket, all bots OR'd (4 total),
  │                      payload shared across colos via KV (activity:v1)
  └─ each route stale-while-revalidate from the colo cache, served at api.tend-src.com
```

## One-time setup (already done)

```sh
npm install
npx wrangler login                                  # opens browser
npx wrangler kv namespace create CACHE              # prints the id
#   → paste the id into wrangler.toml ([[kv_namespaces]] id)
npx wrangler secret put GITHUB_TOKEN                # paste a read-only PAT
npx wrangler deploy                                 # first deploy
```

The PAT needs `actions:read` + `metadata:read` on public repos. After first
deploy, CI handles subsequent deploys via
[`../.github/workflows/worker-deploy.yaml`](../.github/workflows/worker-deploy.yaml),
which authenticates with the `CLOUDFLARE_API_TOKEN` secret stored in the
`cloudflare-deploy` GitHub environment (pinned to `main`, so PR-triggered
workflow runs can't read it). That secret is a scoped token named
`tend-ci-worker-deploy` (Workers Scripts + KV + Routes edit), generated to
keep the account's Global API Key out of CI; regenerate at
<https://dash.cloudflare.com/profile/api-tokens> with the "Edit Cloudflare
Workers" template if it's ever lost.

## Local development

```sh
echo "GITHUB_TOKEN=$(gh auth token)" > .dev.vars   # one-time
npm install
npm run dev        # wrangler dev with hot reload at http://localhost:8787
npm test           # unit tests (vitest, no Worker runtime needed)
npm run typecheck
```

Then `curl http://localhost:8787/activity` etc. `wrangler dev` reads the same
`wrangler.toml`; `.dev.vars` is gitignored.

## Cache strategy

- `caches.default` (Cloudflare's colo cache), keyed by the normalized request
  URL — stores the rendered response. The browser revalidates after the
  freshness budget (`Cache-Control: max-age`); the colo cache, a shared
  cache, retains the entry for ten freshness budgets (`s-maxage`) so SWR
  keeps working between viewers. A `x-tend-stale-at` header on the cached
  response tells `serveCached` when to background-refresh a hit. A missing
  or garbled stamp counts as stale, so the first hit self-heals an entry
  written by code predating this scheme.
- `CACHE` KV namespace, two keys. `repos:v1` (TTL 1 h) holds the
  `consumers.json` content, decoupling `running-tend`'s weekly refresh from
  Worker deploys. `activity:v1` (TTL = 10 freshness budgets) holds the rendered
  `/activity` payload plus its `staleAt`, the shared tier that lets one colo's
  refresh serve the others. KV suits both: the TTLs clear its 60 s minimum, and
  the whole point is cross-isolate, cross-colo sharing.

Concurrent stale-hits are coalesced within a colo: the first request pushes the
cached entry's `x-tend-stale-at` forward by a short grace window
(`REFRESH_GRACE_MS`, currently 30 s) and starts the background refresh;
viewers arriving within that window read the bumped entry as fresh and
skip starting their own refresh. One refresh per stale window per colo,
not one per viewer. `refreshShared` extends this across colos: a refresh
(cold or background) returns a sibling colo's still-fresh `activity:v1` entry
instead of fanning out, and publishes its own result there when it does fan
out.

For `/currently-tending` a cold colo cache costs the full N actions/runs
fanout, bounded to one cold refresh per budget per colo. For `/activity` the
KV tier collapses that to roughly one 4-request Search refresh per budget
across the whole network: a cold or stale colo reads `activity:v1` first and
refreshes only
when KV is stale too, so neither colo count nor traffic multiplies the GitHub
load.

The zone's **Browser Cache TTL** must be set to "Respect Existing Headers"
(value `0`). Any positive value is treated as a floor on outgoing
`max-age` — the Free plan's 4 h default silently rewrites the response on
the way out, pinning browsers to the first snapshot they fetched and
defeating the per-route `max-age` chosen here. `s-maxage` is left alone,
so the colo cache stays correct; only the browser view is affected.
