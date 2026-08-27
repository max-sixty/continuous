// Cloudflare Worker that serves the tend website's live data streams.
//
// Two routes, both CORS-enabled JSON, each with its own freshness budget:
//
//   /currently-tending   30 s   in-progress tend-* workflow runs
//   /activity            5 min  recent PRs / issues / reviews / comments +
//                               lifetime counts, per primitive bucket
//
// Both read the consumer list (`consumers.json`) from the repo, KV-cached
// for an hour, and fan out to GitHub. Responses are stale-while-revalidate:
// a request is always answered from the colo cache (no waiting on the
// fanout) and, when the cached entry is past its budget, also kicks off a
// background refresh so the next request sees fresher data. A background
// refresh only fires on a request, so idle days still cost zero GitHub calls.
//
// The colo cache is per-data-center, so each colo would otherwise fan out to
// GitHub on its own. `/activity` therefore also publishes its rendered
// payload to KV (a global store): a refresh in one colo populates KV for the
// others, so the GitHub fanout fires roughly once per freshness budget across
// the whole network instead of once per colo. KV survives deploys and outlives
// the colo cache, so a viewer waits on GitHub only when colo cache and KV are
// both empty (the first request ever, or after a long idle). `/currently-tending`
// skips KV: its 30s budget is under KV's 60s floor, and its fanout is cheap
// core REST, not the Search quota that the sharing protects.
//
// `/activity` uses one Search query per bucket across all bots. Each page
// yields recent items and the lifetime `total_count`; "this week" is counted
// off the page, so it saturates around one page (~100) per bucket. Four
// concurrent Search requests stay below the 30/minute cap, and KV sharing
// keeps refresh frequency independent of colo count and traffic.
//
// See ../README.md for architecture and the rate-limit reasoning behind
// the budgets.

interface Env {
  GITHUB_TOKEN: string;
  CACHE: KVNamespace;
  ALLOWED_ORIGIN: string;
  REPOS_URL: string;
}

interface Consumer {
  repo: string; // "owner/name"
  bot_name: string;
}

interface WorkflowRun {
  name?: string;
  display_title?: string;
  run_started_at?: string;
  html_url?: string;
  pull_requests?: Array<{ number?: number } | null> | null;
}

interface RunsResponse {
  workflow_runs?: WorkflowRun[];
}

interface CurrentlyTendingEntry {
  repo: string;
  workflow: string;
  // PR/issue title for PR-/issue-triggered runs. Omitted when GitHub's
  // `display_title` is just the workflow name (scheduled runs like
  // nightly/weekly add nothing extra there).
  display_title?: string;
  // PR number when the trigger is a pull_request_* event. Issues events
  // don't populate `pull_requests`, so triage rows carry display_title
  // without a number.
  pr_number?: number;
  started_at: string;
  run_url: string;
}

interface CurrentlyTendingResponse {
  generated_at: string;
  currently_tending: CurrentlyTendingEntry[];
}

// /activity: one bucket per primitive Search query, named off the query.
type ActivityBucketName = "prs" | "issues" | "reviews" | "comments";

interface RecentItem {
  repo: string; // "owner/name"
  title: string;
  url: string;
  at: string; // issue/PR updated_at, ISO
}

interface ActivityBucket {
  count: number; // lifetime — the combined query's Search total_count
  count_this_week: number; // last 7 days; saturates ~one page (100) per bucket
  recent: RecentItem[]; // newest-first, merged across bots
}

type ActivityResponse = {
  generated_at: string;
} & Record<ActivityBucketName, ActivityBucket>;

interface SearchItem {
  html_url: string;
  title: string;
  updated_at: string;
  repository_url: string;
  number: number;
}

interface SearchResponse {
  total_count?: number;
  items?: SearchItem[];
}

// GitHub comment objects — fields we actually use. Same shape for inline
// PR review comments (`/pulls/{n}/comments`) and conversation comments
// (`/issues/{n}/comments`).
interface IssueCommentObject {
  user?: { login?: string } | null;
  html_url?: string;
  created_at?: string;
}

const REPOS_KEY = "repos:v1";
const REPOS_TTL_SECONDS = 3600;
// /activity's rendered payload, shared across colos so one colo's fanout
// spares the rest. See refreshShared.
const ACTIVITY_KV_KEY = "activity:v1";
const FETCH_TIMEOUT_MS = 10_000;
const WORKFLOW_PREFIX = "tend-";
// `actions/runs` sorts by created_at desc across ALL workflows in the
// repo, then we filter to tend-* client-side. 30 (GitHub's default) is
// cheap and avoids tend runs being pushed off by busier non-tend traffic.
const PER_PAGE_RUNS = 30;
// /activity: one Search page per bucket, covering every bot. 100 is Search's
// max page and one request; we keep the newest RECENT_PER_BUCKET for the feed
// and count the rest of the page towards "this week".
const SEARCH_PAGE = 100;
const RECENT_PER_BUCKET = 10;
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const GITHUB_API = "https://api.github.com";
const USER_AGENT = "tend-website-worker";

// tend's own bookkeeping issues — "Bot temporarily unavailable" outage
// trackers, the monthly review-runs / review-reviewers trackers, nightly
// drift-cleanup notes — carry these labels. The `issues` bucket excludes
// them so its count reflects issues filed about the repo, not tend's
// internal record-keeping. A repo without a label just matches nothing.
const BOOKKEEPING_LABELS = [
  "tend-outage",
  "tend-rate-limit",
  "review-runs-tracking",
  "review-reviewers-tracking",
  "nightly-cleanup",
];
// Repeat a Search qualifier once per value. Positive qualifiers OR
// (`author:a author:b` matches either); negated ones AND (`-label:x -label:y`
// excludes both).
const qualify = (qualifier: string, values: string[]) =>
  values.map((v) => `${qualifier}:${v}`).join(" ");

const ISSUE_LABEL_FILTER = qualify("-label", BOOKKEEPING_LABELS);

// Why primitive buckets, not a job taxonomy: GitHub records mechanical facts
// (PR opened, review submitted, comment created), but tend's jobs (review /
// triage / ci-fix / nightly / weekly) don't map onto them cleanly — a PR on
// `fix/ci-*` vs `fix/issue-*` vs `tend/update-workflows` is the same event
// type, and a nightly survey that finds nothing leaves no trace. An earlier
// cut reverse-engineered a `kind` enum from branch names and still
// mislabelled things. The site renders the mechanical buckets directly; the
// "what's tend been up to" narrative is deferred to a Phase 2 LLM summary
// (see TODO.md).
//
// Each bucket is ONE Search request covering every bot, not one per bot: the
// whole consumer list folds into a single query via `qualify`, so a refresh
// costs 4 Search requests whatever the consumer count. That bound is the
// point: Search allows 30 requests/minute, so the old bot-by-bot fanout
// (4·N) crossed the limit at 8 consumers and every refresh after that would
// have sunk — see searchIssues. Negated qualifiers AND rather than OR, so the
// `comments` exclusions drop anything authored/reviewed by *any* bot.
//
// What still grows with the consumer count is query *length* — `comments` is
// the longest at three qualifiers per bot, ~570 characters at 8 consumers.
// That, not the request count, is what bounds this shape now.
//
// `q` for each /activity bucket — "some bot …":
const BUCKET_QUERIES: Record<ActivityBucketName, (bots: string[]) => string> = {
  prs: (bs) => `${qualify("author", bs)} is:pr`, // …opened these PRs
  issues: (bs) => `${qualify("author", bs)} is:issue ${ISSUE_LABEL_FILTER}`, // …opened these issues (minus its own bookkeeping)
  reviews: (bs) => qualify("reviewed-by", bs), // …reviewed these PRs (approve / request-changes / review comment)
  comments: (bs) =>
    `${qualify("commenter", bs)} ${qualify("-author", bs)} ${qualify("-reviewed-by", bs)}`, // …commented on these PRs/issues (not a bot's own, not folded in from a review)
};

// Per-route freshness budgets, in seconds. `ok` applies to a good refresh;
// `fallback` (used when the refresh throws) is shorter so a transient
// outage clears quickly. Past its budget a cached entry is still served —
// see serveCached's stale-while-revalidate — until it's STALE_SERVE_FACTOR
// budgets old, at which point the colo cache drops it.
const TTL = {
  "currently-tending": { ok: 30, fallback: 5 },
  activity: { ok: 300, fallback: 30 },
} as const;

// Multiple of the freshness budget that a cached entry stays serveable
// before the colo cache evicts it. Bounds how stale a viewer can see; the
// larger it is, the longer a quiet site stays warm between refreshes.
const STALE_SERVE_FACTOR = 10;

// serveCached stamps each cached response with the instant (epoch ms) past
// which a hit should trigger a background refresh.
const STALE_AT_HEADER = "x-tend-stale-at";

// How far forward a stale-hit pushes its cached entry's stale-at before
// firing the background refresh. Concurrent stale-hits within this window
// read the bumped entry as fresh and skip starting their own refresh, so
// the colo runs one refresh per stale window instead of one per viewer.
// Comfortably above the FETCH_TIMEOUT_MS=10s worst-case refresh time; the
// real refresh's put overwrites the bumped entry as soon as it completes.
const REFRESH_GRACE_MS = 30_000;

// owner/name — alphanumerics + `_-.`, no leading `.`/`-`, no `..` anywhere,
// exactly one slash.
const REPO_PART = /^[A-Za-z0-9_][A-Za-z0-9._-]*$/;
function isValidRepo(repo: string): boolean {
  if (repo.includes("..")) return false;
  const parts = repo.split("/");
  return parts.length === 2 && parts.every((p) => REPO_PART.test(p));
}

// GitHub bot usernames are unconstrained enough that we validate before
// interpolating into a Search query. Letters/digits/`-`/`_`, max 39 chars
// (GitHub's own cap).
const BOT_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,38}$/;
function isValidBotName(name: string): boolean {
  return BOT_NAME.test(name);
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === "OPTIONS") {
      return corsPreflight(env);
    }
    if (request.method !== "GET") {
      return withCors(new Response("Method Not Allowed", { status: 405 }), env);
    }

    const url = new URL(request.url);
    switch (url.pathname) {
      case "/":
      case "/currently-tending":
        return serveCached(url, env, ctx, {
          cacheKeyPath: "/currently-tending",
          ttl: TTL["currently-tending"],
          refresh: () => refreshCurrentlyTending(env),
        });
      case "/activity":
        return serveCached(url, env, ctx, {
          cacheKeyPath: "/activity",
          ttl: TTL.activity,
          kvKey: ACTIVITY_KV_KEY,
          refresh: () => refreshActivity(env),
        });
      default:
        return withCors(new Response("Not Found", { status: 404 }), env);
    }
  },
};

// ---------------------------------------------------------------------------
// Cache-and-serve

interface CacheOpts<T> {
  cacheKeyPath: string;
  ttl: { ok: number; fallback: number };
  refresh: () => Promise<T>;
  // When set, refreshes coordinate through this KV key (a global store) so a
  // fanout in one colo populates KV for every other colo. Routes without it
  // (currently-tending, whose 30s budget is under KV's 60s floor) refresh
  // straight from GitHub on each colo.
  kvKey?: string;
}

// A KV-stored payload plus the instant it goes stale (epoch ms). The colo
// cache is the hot, per-colo tier; KV is the shared tier behind it, so one
// colo's refresh serves the others.
interface KvEntry<T> {
  payload: T;
  staleAt: number;
}

function readKvEntry<T>(env: Env, key: string): Promise<KvEntry<T> | null> {
  return env.CACHE.get<KvEntry<T>>(key, "json").catch(() => null);
}

// Refresh that coordinates through KV when opts.kvKey is set: if a sibling
// colo wrote a still-fresh entry, reuse it and skip GitHub; otherwise fan out
// and publish the result to KV for the others. Without a kvKey it's a straight
// GitHub fanout. Both the cold path and the background refresh funnel through
// here, so the GitHub fanout fires at most once per freshness budget across
// every colo except those racing within KV's write-propagation window.
//
// Returns the entry, not just the payload: a reused KV entry carries its
// original stale-at, a freshly-fetched one `now + ttl.ok`. Callers stamp the
// colo cache from that, so a colo inherits the shared clock instead of
// resetting it (see entryResponse).
async function refreshShared<T>(env: Env, opts: CacheOpts<T>): Promise<KvEntry<T>> {
  // staleAt is read after the fanout resolves (property order), so a slow
  // fanout doesn't shorten the published freshness window.
  const mint = async (): Promise<KvEntry<T>> => ({
    payload: await opts.refresh(),
    staleAt: Date.now() + opts.ttl.ok * 1000,
  });
  if (!opts.kvKey) return mint();
  const entry = await readKvEntry<T>(env, opts.kvKey);
  if (entry && Date.now() < entry.staleAt) return entry; // sibling colo's fresh write
  const fresh = await mint();
  await env.CACHE.put(opts.kvKey, JSON.stringify(fresh), {
    // Outlive the freshness budget by STALE_SERVE_FACTOR, matching the colo
    // cache, so a quiet stretch still leaves a (stale) entry to serve. Stays
    // above KV's 60s minimum expirationTtl for any route whose ttl.ok ≥ 6s
    // (activity's is 300s); a shorter-budget route would need its own floor.
    expirationTtl: opts.ttl.ok * STALE_SERVE_FACTOR,
  }).catch((e) => console.error(`KV put failed for ${opts.kvKey}:`, e));
  return fresh;
}

async function serveCached<T>(
  url: URL,
  env: Env,
  ctx: ExecutionContext,
  opts: CacheOpts<T>,
): Promise<Response> {
  // Normalize the cache key so query strings don't fork the cache. The colo
  // cache (not KV) holds the rendered Response: its sub-second granularity
  // suits currently-tending's 30s budget, below KV's 60s floor. /activity
  // shares its payload across colos via KV behind this hot tier (refreshShared).
  const cacheKey = new Request(`${url.origin}${opts.cacheKeyPath}`, {
    method: "GET",
  });
  const cached = await caches.default.match(cacheKey).catch(() => undefined);

  if (cached) {
    // Stale-while-revalidate: answer from cache immediately — never make a
    // viewer wait on the GitHub fanout. Past its freshness budget, kick
    // off a background refresh so the next request gets fresher data.
    // coalesceAndRefresh first pushes the entry's stale-at forward by
    // REFRESH_GRACE_MS so concurrent stale-hits within that window read
    // the entry as fresh and skip starting their own refresh — one refresh
    // per stale window per colo instead of one per viewer.
    if (isStale(cached.headers.get(STALE_AT_HEADER), Date.now())) {
      ctx.waitUntil(
        coalesceAndRefresh(cacheKey, cached, env, opts).catch((e) =>
          console.error(
            `background refresh failed for ${opts.cacheKeyPath}:`,
            e,
          ),
        ),
      );
    }
    return cached;
  }

  // Cold colo cache — a fresh isolate, or no traffic for STALE_SERVE_FACTOR
  // budgets. refreshAndCache checks KV before paying the fanout.
  return refreshAndCache(cacheKey, env, ctx, opts);
}

// Cold colo-cache path. For a KV-shared route, a sibling colo may already
// have the data in KV: serve that (even when stale) so this viewer skips the
// GitHub fanout, and a burst after idle doesn't make every colo fan out at
// once. A stale KV entry is served immediately and revalidated in the
// background, so an outage on cold start shows the last-known data rather than
// nothing.
//
// Only when colo cache and KV are both empty does this request pay the fanout.
// On failure there's then no prior data, so we return a 503 rather than a 200
// all-zero payload: the site reads the non-OK response as "no data" and hides
// the section instead of rendering fabricated zeros. The 503 is negative-cached
// at the short fallback budget so a sustained outage doesn't re-fan-out on
// every request; a later stale hit revalidates and swaps in real data once the
// source recovers. (Warm-cache failures never reach here — coalesceAndRefresh
// keeps the last good entry, so an outage never overwrites good data.)
async function refreshAndCache<T>(
  cacheKey: Request,
  env: Env,
  ctx: ExecutionContext,
  opts: CacheOpts<T>,
): Promise<Response> {
  if (opts.kvKey) {
    const entry = await readKvEntry<T>(env, opts.kvKey);
    if (entry) {
      const response = entryResponse(entry, env, opts.ttl.ok);
      if (Date.now() >= entry.staleAt) {
        // coalesceAndRefresh writes the colo entry (bumped, then fresh) and
        // re-checks KV, so don't also put here.
        ctx.waitUntil(
          coalesceAndRefresh(cacheKey, response.clone(), env, opts).catch((e) =>
            console.error(`background refresh failed for ${opts.cacheKeyPath}:`, e),
          ),
        );
      } else {
        ctx.waitUntil(
          caches.default
            .put(cacheKey, response.clone())
            .catch((e) => console.error(`cache put failed for ${opts.cacheKeyPath}:`, e)),
        );
      }
      return response;
    }
  }

  let response: Response;
  try {
    response = entryResponse(await refreshShared(env, opts), env, opts.ttl.ok);
  } catch (e) {
    console.error(`cold refresh failed for ${opts.cacheKeyPath}:`, e);
    response = jsonResponse(
      { error: "upstream unavailable" },
      env,
      opts.ttl.fallback,
      503,
    );
  }
  ctx.waitUntil(
    caches.default
      .put(cacheKey, response.clone())
      .catch((e) => console.error(`cache put failed for ${opts.cacheKeyPath}:`, e)),
  );
  return response;
}

// Stale-hit path: coalesce concurrent refreshes by bumping the cached
// entry's stale-at forward before running the refresh. Concurrent
// stale-hits arriving within REFRESH_GRACE_MS read the bumped entry as
// fresh and skip starting their own refresh. The refresh goes through
// refreshShared, so on a KV-shared route it pulls a sibling colo's fresh KV
// entry instead of fanning out. On refresh failure keep the bumped cached
// entry — a viewer keeps seeing the previous (slightly stale) data instead of
// an empty payload overwriting it for the fallback TTL, which would briefly
// black out every viewer.
async function coalesceAndRefresh<T>(
  cacheKey: Request,
  cached: Response,
  env: Env,
  opts: CacheOpts<T>,
): Promise<void> {
  const bumped = new Response(cached.clone().body, {
    status: cached.status,
    headers: new Headers(cached.headers),
  });
  bumped.headers.set(STALE_AT_HEADER, String(Date.now() + REFRESH_GRACE_MS));
  await caches.default.put(cacheKey, bumped);
  try {
    const fresh = entryResponse(await refreshShared(env, opts), env, opts.ttl.ok);
    await caches.default.put(cacheKey, fresh);
  } catch (e) {
    console.error(
      `background refresh failed for ${opts.cacheKeyPath}; keeping stale entry:`,
      e,
    );
  }
}

// A cached entry past this instant (epoch ms, from STALE_AT_HEADER) is
// still served but triggers a background refresh. A missing or garbled
// stamp counts as stale, so an entry written before this scheme — or any
// the cache mangles — gets refreshed promptly.
function isStale(staleAtHeader: string | null, nowMs: number): boolean {
  const staleAt = Number(staleAtHeader);
  return !Number.isFinite(staleAt) || nowMs >= staleAt;
}

// ---------------------------------------------------------------------------
// /currently-tending

async function refreshCurrentlyTending(env: Env): Promise<CurrentlyTendingResponse> {
  const consumers = await getConsumers(env);
  const perRepo = await Promise.all(
    consumers.map((c) => fetchRepoRuns(c.repo, env.GITHUB_TOKEN)),
  );
  const entries = perRepo.flat();
  entries.sort((a, b) => {
    if (a.started_at !== b.started_at) {
      return a.started_at < b.started_at ? 1 : -1;
    }
    return a.repo < b.repo ? -1 : 1;
  });
  return { generated_at: nowIso(), currently_tending: entries };
}

async function fetchRepoRuns(
  repo: string,
  token: string,
): Promise<CurrentlyTendingEntry[]> {
  if (!isValidRepo(repo)) {
    console.error(`skipping malformed repo: ${repo}`);
    return [];
  }
  const url =
    `${GITHUB_API}/repos/${repo}/actions/runs` +
    `?status=in_progress&per_page=${PER_PAGE_RUNS}`;
  const resp = await fetchWithTimeout(url, { headers: githubHeaders(token) });
  if (!resp.ok) {
    if (resp.status === 401 || resp.status === 403) {
      throw new Error(`auth failure for ${repo}: ${resp.status}`);
    }
    console.error(`runs fetch skipped for ${repo}: ${resp.status}`);
    return [];
  }
  const data = (await resp.json()) as RunsResponse;
  return (data.workflow_runs ?? [])
    .filter(
      (
        run,
      ): run is WorkflowRun & {
        name: string;
        run_started_at: string;
        html_url: string;
      } =>
        typeof run.name === "string" &&
        run.name.startsWith(WORKFLOW_PREFIX) &&
        typeof run.run_started_at === "string" &&
        typeof run.html_url === "string",
    )
    .map((run) => {
      const entry: CurrentlyTendingEntry = {
        repo,
        workflow: run.name,
        started_at: run.run_started_at,
        run_url: run.html_url,
      };
      // GitHub falls `display_title` back to the workflow name for events
      // with no event-specific title (schedule, workflow_dispatch). Drop
      // those — they'd render as "nightly · nightly".
      if (typeof run.display_title === "string" && run.display_title !== run.name) {
        entry.display_title = run.display_title;
      }
      const prNumber = run.pull_requests?.[0]?.number;
      if (typeof prNumber === "number") {
        entry.pr_number = prNumber;
      }
      return entry;
    });
}

// ---------------------------------------------------------------------------
// /activity

async function refreshActivity(env: Env): Promise<ActivityResponse> {
  const out = emptyActivity();
  const bots = botNames(await getConsumers(env));
  if (bots.length === 0) return out;
  const weekAgoMs = Date.now() - WEEK_MS;

  await Promise.all(
    (Object.keys(BUCKET_QUERIES) as ActivityBucketName[]).map(async (name) => {
      const page = await searchIssues(BUCKET_QUERIES[name](bots), env.GITHUB_TOKEN);
      const items = page.items ?? [];
      let countThisWeek = 0;
      for (const it of items) {
        if (Date.parse(it.updated_at) >= weekAgoMs) countThisWeek++;
      }
      items.sort((a, b) =>
        a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0,
      );
      const top = items.slice(0, RECENT_PER_BUCKET);
      const recent = await Promise.all(
        top.map((it) => toRecentItem(name, it, bots, env.GITHUB_TOKEN)),
      );
      out[name] = {
        count: page.total_count ?? 0,
        count_this_week: countThisWeek,
        recent,
      };
    }),
  );
  return out;
}

// `prs`/`issues` rows link to the parent — that IS what the bot created.
// `reviews`/`comments` rows link to the bot's specific review/comment so
// clicking lands on tend's actual action, not the top of the thread.
// Follow-up failure (404, transient error, race where Search saw the action
// but the comment isn't queryable yet) falls back to the parent URL — better
// than dropping the row.
// A combined query doesn't say which bot matched, so the follow-up matches
// against the whole consumer list. It reads the same single page either way —
// no extra requests — and only one bot acts in a given repo in practice.
async function toRecentItem(
  bucket: ActivityBucketName,
  it: SearchItem,
  bots: string[],
  token: string,
): Promise<RecentItem> {
  const repo = repoFromApiUrl(it.repository_url);
  const base = {
    repo,
    title: it.title,
    at: it.updated_at,
  };
  if (bucket === "reviews") {
    const deep = await findBotReviewUrl(repo, it.number, bots, token);
    return { ...base, url: deep ?? it.html_url };
  }
  if (bucket === "comments") {
    const deep = await findBotCommentUrl(repo, it.number, bots, token);
    return { ...base, url: deep ?? it.html_url };
  }
  return { ...base, url: it.html_url };
}

// Latest inline review comment on a PR authored by one of `bots` —
// `created_at` desc.
// We deliberately don't anchor on the review summary (`#pullrequestreview-…`):
// tend's reviews are typically `COMMENTED` with an empty body wrapping inline
// comments, so the review anchor scrolls nowhere on the conversation page.
// `#discussion_r…` anchors land on the actual comment thread. Returns null if
// the fetch fails or the bot left no inline comments (caller falls back to
// the parent PR URL).
async function findBotReviewUrl(
  repo: string,
  n: number,
  bots: string[],
  token: string,
): Promise<string | null> {
  const url = `${GITHUB_API}/repos/${repo}/pulls/${n}/comments?per_page=100`;
  try {
    const resp = await fetchWithTimeout(url, { headers: githubHeaders(token) });
    if (!resp.ok) {
      console.error(`review-comment lookup failed (${resp.status}): ${repo}#${n}`);
      return null;
    }
    const comments = (await resp.json()) as IssueCommentObject[];
    return latestByBot(
      comments,
      bots,
      (c) => c.created_at,
      (c) => c.html_url,
    );
  } catch (e) {
    console.error(`review-comment lookup error: ${repo}#${n}`, e);
    return null;
  }
}

// Latest issue/PR-conversation comment authored by one of `bots` —
// `created_at` desc. (Inline PR review comments live at a different endpoint;
// the comments bucket excludes `reviewed-by:<bot>`, so the bot's contribution
// is an issue-style comment.) Returns null on failure (caller falls back).
async function findBotCommentUrl(
  repo: string,
  n: number,
  bots: string[],
  token: string,
): Promise<string | null> {
  const url = `${GITHUB_API}/repos/${repo}/issues/${n}/comments?per_page=100`;
  try {
    const resp = await fetchWithTimeout(url, { headers: githubHeaders(token) });
    if (!resp.ok) {
      console.error(`comment lookup failed (${resp.status}): ${repo}#${n}`);
      return null;
    }
    const comments = (await resp.json()) as IssueCommentObject[];
    return latestByBot(
      comments,
      bots,
      (c) => c.created_at,
      (c) => c.html_url,
    );
  } catch (e) {
    console.error(`comment lookup error: ${repo}#${n}`, e);
    return null;
  }
}

function latestByBot<T extends { user?: { login?: string } | null }>(
  entries: T[],
  bots: string[],
  ts: (e: T) => string | undefined,
  href: (e: T) => string | undefined,
): string | null {
  let bestTs = "";
  let bestUrl: string | null = null;
  for (const e of entries) {
    const login = e.user?.login;
    if (login === undefined || !bots.includes(login)) continue;
    const t = ts(e);
    const h = href(e);
    if (typeof t !== "string" || typeof h !== "string") continue;
    if (t > bestTs) {
      bestTs = t;
      bestUrl = h;
    }
  }
  return bestUrl;
}

function emptyActivity(): ActivityResponse {
  return {
    generated_at: nowIso(),
    prs: { count: 0, count_this_week: 0, recent: [] },
    issues: { count: 0, count_this_week: 0, recent: [] },
    reviews: { count: 0, count_this_week: 0, recent: [] },
    comments: { count: 0, count_this_week: 0, recent: [] },
  };
}

function repoFromApiUrl(repositoryUrl: string): string {
  // https://api.github.com/repos/owner/name -> owner/name
  const i = repositoryUrl.indexOf("/repos/");
  return i === -1 ? "" : repositoryUrl.slice(i + "/repos/".length);
}

// ---------------------------------------------------------------------------
// Search API

// One Search page, newest-first — yields both `items` (recent) and
// `total_count` (lifetime). Any non-OK status throws, sinking the refresh: a
// background refresh keeps the last good cached entry, a cold refresh
// short-fallbacks (30s) and retries. We do NOT degrade a failure to `{}` —
// that cached an all-zero payload as a successful refresh at the full budget,
// pinning "0 PRs / 0 reviews / …" on the site for up to the stale-serve window
// on a transient Search error (a 429, a 5xx blip). A real empty bucket is a
// 200 with `total_count: 0`, so genuine zeros still pass.
//
// Refusing to degrade only survives a *transient* failure: the fallback TTL
// is 30s, so a structural failure re-attempts about twice a minute and never
// drains. That is why BUCKET_QUERIES keeps the refresh at 4 requests — a
// fanout that sits above Search's 30/minute sinks every refresh, and the
// site's sections hide themselves rather than erroring, so it shows as a
// blank page rather than an alarm.
async function searchIssues(query: string, token: string): Promise<SearchResponse> {
  const params = new URLSearchParams({
    q: query,
    per_page: String(SEARCH_PAGE),
    sort: "updated",
    order: "desc",
  });
  const resp = await fetchWithTimeout(`${GITHUB_API}/search/issues?${params}`, {
    headers: githubHeaders(token),
  });
  if (!resp.ok) {
    throw new Error(`search request failed (${resp.status}): ${query}`);
  }
  return (await resp.json()) as SearchResponse;
}

// ---------------------------------------------------------------------------
// Consumers

async function getConsumers(env: Env): Promise<Consumer[]> {
  const cached = await env.CACHE.get<Consumer[]>(REPOS_KEY, "json").catch(() => null);
  if (cached) return cached;

  const resp = await fetchWithTimeout(env.REPOS_URL, {
    cf: { cacheTtl: REPOS_TTL_SECONDS },
  });
  if (!resp.ok) {
    throw new Error(`consumers.json fetch failed: ${resp.status}`);
  }
  const raw = await resp.json();
  if (!isConsumerArray(raw)) {
    throw new Error("consumers.json failed shape validation");
  }
  await env.CACHE.put(REPOS_KEY, JSON.stringify(raw), {
    expirationTtl: REPOS_TTL_SECONDS,
  }).catch((e) => console.error("repos KV put failed:", e));
  return raw;
}

function isConsumerArray(v: unknown): v is Consumer[] {
  return (
    Array.isArray(v) &&
    v.every(
      (e) =>
        typeof e === "object" &&
        e !== null &&
        typeof (e as { repo: unknown }).repo === "string" &&
        isValidRepo((e as { repo: string }).repo) &&
        typeof (e as { bot_name: unknown }).bot_name === "string" &&
        isValidBotName((e as { bot_name: string }).bot_name),
    )
  );
}

function botNames(consumers: Consumer[]): string[] {
  return Array.from(new Set(consumers.map((c) => c.bot_name))).sort();
}

// ---------------------------------------------------------------------------
// HTTP

async function fetchWithTimeout(input: string, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function githubHeaders(token: string): HeadersInit {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": USER_AGENT,
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function withCors(resp: Response, env: Env): Response {
  resp.headers.set("Access-Control-Allow-Origin", env.ALLOWED_ORIGIN);
  return resp;
}

function cacheableJson(
  data: unknown,
  env: Env,
  cache: { staleAtMs: number; maxAgeSeconds: number; sMaxAgeSeconds: number },
  status = 200,
): Response {
  return withCors(
    new Response(JSON.stringify(data), {
      status,
      headers: {
        "Content-Type": "application/json",
        // Browsers revalidate after `max-age`; the colo cache, a shared
        // cache, keeps the entry for `s-maxage` so it stays warm for
        // stale-while-revalidate. Requires the zone's Browser Cache TTL to be
        // "Respect Existing Headers" — any positive value there acts as a floor
        // on outgoing `max-age`, so a 4 h default silently inflates this header
        // and pins browsers to whichever snapshot they fetched first.
        "Cache-Control": `public, max-age=${cache.maxAgeSeconds}, s-maxage=${cache.sMaxAgeSeconds}`,
        [STALE_AT_HEADER]: String(cache.staleAtMs),
      },
    }),
    env,
  );
}

// A freshly-fetched payload: the budget starts ticking now.
function jsonResponse(
  data: unknown,
  env: Env,
  ttlSeconds: number,
  status = 200,
): Response {
  return cacheableJson(
    data,
    env,
    {
      staleAtMs: Date.now() + ttlSeconds * 1000,
      maxAgeSeconds: ttlSeconds,
      sMaxAgeSeconds: ttlSeconds * STALE_SERVE_FACTOR,
    },
    status,
  );
}

// A response built from a cache entry (a fresh fetch or a sibling colo's KV
// write). It inherits the entry's stale-at, so a colo never resets the shared
// clock: an entry written 200 s ago goes stale at its original instant, not a
// full budget from now. max-age is the entry's remaining freshness, floored at
// 0 for a stale entry, which serveCached then revalidates. s-maxage keeps the
// full colo-retention window regardless.
function entryResponse<T>(entry: KvEntry<T>, env: Env, okTtl: number): Response {
  return cacheableJson(entry.payload, env, {
    staleAtMs: entry.staleAt,
    maxAgeSeconds: Math.max(0, Math.ceil((entry.staleAt - Date.now()) / 1000)),
    sMaxAgeSeconds: okTtl * STALE_SERVE_FACTOR,
  });
}

function corsPreflight(env: Env): Response {
  return withCors(
    new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Max-Age": "86400",
      },
    }),
    env,
  );
}

// Exported for unit tests.
export const __test = {
  refreshCurrentlyTending,
  refreshActivity,
  fetchRepoRuns,
  getConsumers,
  isConsumerArray,
  isValidRepo,
  isValidBotName,
  findBotReviewUrl,
  findBotCommentUrl,
  isStale,
  coalesceAndRefresh,
  refreshAndCache,
  refreshShared,
  ACTIVITY_KV_KEY,
  STALE_AT_HEADER,
};
