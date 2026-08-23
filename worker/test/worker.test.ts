import { describe, expect, it, vi, afterEach } from "vitest";

const originalFetch = globalThis.fetch;

interface RunsResponse {
  workflow_runs: Array<{
    name: string;
    display_title?: string;
    run_started_at: string;
    html_url: string;
    pull_requests?: Array<{ number: number }>;
  }>;
}

function makeFetch(responses: Map<string, unknown>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (!responses.has(url)) {
      throw new Error(`unexpected fetch ${url}`);
    }
    return new Response(JSON.stringify(responses.get(url)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("fetchRepoRuns", () => {
  it("filters to tend-* workflows and shapes the entry", async () => {
    const { __test } = await import("../src/index");
    const responses = new Map<string, unknown>([
      [
        "https://api.github.com/repos/o/r/actions/runs?status=in_progress&per_page=30",
        {
          workflow_runs: [
            {
              name: "tend-review",
              display_title: "Fix the bug",
              run_started_at: "2026-05-10T17:00:00Z",
              html_url: "https://github.com/o/r/actions/runs/1",
              pull_requests: [{ number: 42 }],
            },
            {
              name: "ci",
              run_started_at: "2026-05-10T17:01:00Z",
              html_url: "https://github.com/o/r/actions/runs/2",
            },
            {
              name: "tend-triage",
              display_title: "Bug: thing is broken",
              run_started_at: "2026-05-10T17:02:00Z",
              html_url: "https://github.com/o/r/actions/runs/3",
              pull_requests: [],
            },
            {
              name: "tend-nightly",
              // schedule runs have display_title === name; should drop it.
              display_title: "tend-nightly",
              run_started_at: "2026-05-10T17:03:00Z",
              html_url: "https://github.com/o/r/actions/runs/4",
              pull_requests: [],
            },
          ],
        } satisfies RunsResponse,
      ],
    ]);
    globalThis.fetch = makeFetch(responses) as unknown as typeof fetch;

    const result = await __test.fetchRepoRuns("o/r", "token");
    expect(result).toEqual([
      {
        repo: "o/r",
        workflow: "tend-review",
        display_title: "Fix the bug",
        pr_number: 42,
        started_at: "2026-05-10T17:00:00Z",
        run_url: "https://github.com/o/r/actions/runs/1",
      },
      {
        repo: "o/r",
        workflow: "tend-triage",
        display_title: "Bug: thing is broken",
        started_at: "2026-05-10T17:02:00Z",
        run_url: "https://github.com/o/r/actions/runs/3",
      },
      {
        repo: "o/r",
        workflow: "tend-nightly",
        started_at: "2026-05-10T17:03:00Z",
        run_url: "https://github.com/o/r/actions/runs/4",
      },
    ]);
  });

  it("returns empty on 404 (repo gone) — does not throw", async () => {
    const { __test } = await import("../src/index");
    globalThis.fetch = vi.fn(
      async () => new Response("not found", { status: 404 }),
    ) as unknown as typeof fetch;

    const result = await __test.fetchRepoRuns("o/r", "token");
    expect(result).toEqual([]);
  });

  it("throws on 401/403 (auth problem) — surfaces to caller", async () => {
    const { __test } = await import("../src/index");
    globalThis.fetch = vi.fn(
      async () => new Response("bad credentials", { status: 401 }),
    ) as unknown as typeof fetch;

    await expect(__test.fetchRepoRuns("o/r", "token")).rejects.toThrow(
      /auth failure/,
    );
  });
});

describe("refreshCurrentlyTending", () => {
  it("fans out across consumers and sorts newest first", async () => {
    const { __test } = await import("../src/index");
    const responses = new Map<string, unknown>([
      [
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
        [
          { repo: "max-sixty/tend", bot_name: "tend-agent" },
          { repo: "PRQL/prql", bot_name: "prql-bot" },
        ],
      ],
      [
        "https://api.github.com/repos/max-sixty/tend/actions/runs?status=in_progress&per_page=30",
        {
          workflow_runs: [
            {
              name: "tend-review",
              run_started_at: "2026-05-10T10:00:00Z",
              html_url: "u1",
            },
          ],
        },
      ],
      [
        "https://api.github.com/repos/PRQL/prql/actions/runs?status=in_progress&per_page=30",
        {
          workflow_runs: [
            {
              name: "tend-triage",
              run_started_at: "2026-05-10T12:00:00Z",
              html_url: "u2",
            },
          ],
        },
      ],
    ]);
    globalThis.fetch = makeFetch(responses) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL:
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
    };
    const out = await __test.refreshCurrentlyTending(env);
    expect(out.currently_tending).toEqual([
      {
        repo: "PRQL/prql",
        workflow: "tend-triage",
        started_at: "2026-05-10T12:00:00Z",
        run_url: "u2",
      },
      {
        repo: "max-sixty/tend",
        workflow: "tend-review",
        started_at: "2026-05-10T10:00:00Z",
        run_url: "u1",
      },
    ]);
    expect(out.generated_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });
});

describe("refreshActivity", () => {
  // Build a Search URL the way searchIssues does: q + per_page + sort + order.
  const searchUrl = (q: string) =>
    `https://api.github.com/search/issues?${new URLSearchParams({
      q,
      per_page: "100",
    })}&sort=updated&order=desc`;

  // The label filter the worker appends to the `issues` bucket query —
  // mirrors BOOKKEEPING_LABELS in src/index.ts. Coupling is deliberate: a
  // change to that list is a behaviour change and should break this test.
  const ISSUE_FILTER =
    "-label:tend-outage -label:tend-rate-limit -label:review-runs-tracking -label:review-reviewers-tracking -label:nightly-cleanup";

  it("issues one Search query per bucket covering every bot — merges + sorts recent, counts this week", async () => {
    const { __test } = await import("../src/index");
    const nowMs = Date.now();
    const daysAgo = (n: number) => new Date(nowMs - n * 86_400_000).toISOString();
    const recentA = daysAgo(1); // within the last 7 days
    const recentB = daysAgo(2);
    const oldA = daysAgo(30); // not
    const oldB = daysAgo(60);

    const item = (
      repo: string,
      n: number,
      kind: "pull" | "issues",
      at: string,
    ) => ({
      html_url: `https://github.com/${repo}/${kind}/${n}`,
      title: `${repo}#${n}`,
      updated_at: at,
      repository_url: `https://api.github.com/repos/${repo}`,
      number: n,
    });

    // For reviews/comments rows, the worker issues one follow-up per recent
    // item to resolve the bot's specific comment anchor. Mock those.
    // Reviews → inline PR review comments (`#discussion_r…`); the review
    // summary anchor is skipped because tend's reviews are typically body-empty.
    const reviewCommentUrl = (repo: string, n: number) =>
      `https://api.github.com/repos/${repo}/pulls/${n}/comments?per_page=100`;
    const commentUrl = (repo: string, n: number) =>
      `https://api.github.com/repos/${repo}/issues/${n}/comments?per_page=100`;
    const reviewComment = (repo: string, n: number, bot: string, id: number, at: string) => ({
      user: { login: bot },
      html_url: `https://github.com/${repo}/pull/${n}#discussion_r${id}`,
      created_at: at,
    });
    const comment = (
      repo: string,
      n: number,
      kind: "pull" | "issues",
      bot: string,
      id: number,
      at: string,
    ) => ({
      user: { login: bot },
      html_url: `https://github.com/${repo}/${kind}/${n}#issuecomment-${id}`,
      created_at: at,
    });

    const responses = new Map<string, unknown>([
      [
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
        [
          { repo: "o/a", bot_name: "bot-a" },
          { repo: "o/b", bot_name: "bot-b" },
        ],
      ],
      // bots sorted → bot-a, bot-b, folded into ONE query per bucket via
      // repeated (OR'd) qualifiers. Buckets in declared order: prs, issues,
      // reviews, comments. Four Search requests total, not 4×2.
      [searchUrl("author:bot-a author:bot-b is:pr"), { total_count: 10, items: [item("o/a", 1, "pull", recentA), item("o/a", 2, "pull", oldA), item("o/b", 9, "pull", oldB)] }],
      [searchUrl(`author:bot-a author:bot-b is:issue ${ISSUE_FILTER}`), { total_count: 2, items: [item("o/a", 5, "issues", recentB)] }],
      [searchUrl("reviewed-by:bot-a reviewed-by:bot-b"), { total_count: 14, items: [item("o/a", 4, "pull", recentA), item("o/b", 7, "pull", oldB)] }],
      [searchUrl("commenter:bot-a commenter:bot-b -author:bot-a -author:bot-b -reviewed-by:bot-a -reviewed-by:bot-b"), { total_count: 16, items: [item("o/a", 3, "pull", recentA), item("o/b", 8, "issues", recentB)] }],
      // Follow-ups: pick the latest entry by the bot; the worker should land on the deepest URL.
      [reviewCommentUrl("o/a", 4), [
        reviewComment("o/a", 4, "someone-else", 100, recentB),
        reviewComment("o/a", 4, "bot-a", 101, recentA),
      ]],
      [reviewCommentUrl("o/b", 7), [reviewComment("o/b", 7, "bot-b", 202, oldB)]],
      [commentUrl("o/a", 3), [
        comment("o/a", 3, "pull", "bot-a", 301, recentB),
        comment("o/a", 3, "pull", "bot-a", 302, recentA), // newest by created_at
      ]],
      [commentUrl("o/b", 8), [comment("o/b", 8, "issues", "bot-b", 401, recentB)]],
    ]);
    globalThis.fetch = makeFetch(responses) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL:
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
    };
    const out = await __test.refreshActivity(env);

    expect(out.prs).toEqual({
      count: 10, // the combined query's total_count
      count_this_week: 1, // o/a#1 recent; o/a#2 and o/b#9 old
      recent: [
        { repo: "o/a", title: "o/a#1", url: "https://github.com/o/a/pull/1", at: recentA },
        { repo: "o/a", title: "o/a#2", url: "https://github.com/o/a/pull/2", at: oldA },
        { repo: "o/b", title: "o/b#9", url: "https://github.com/o/b/pull/9", at: oldB },
      ],
    });
    expect(out.issues).toEqual({
      count: 2,
      count_this_week: 1,
      recent: [
        { repo: "o/a", title: "o/a#5", url: "https://github.com/o/a/issues/5", at: recentB },
      ],
    });
    expect(out.reviews).toEqual({
      count: 14,
      count_this_week: 1, // o/a#4 recent; o/b#7 old
      recent: [
        // url resolved to the bot's most recent inline comment, not the PR top.
        { repo: "o/a", title: "o/a#4", url: "https://github.com/o/a/pull/4#discussion_r101", at: recentA },
        { repo: "o/b", title: "o/b#7", url: "https://github.com/o/b/pull/7#discussion_r202", at: oldB },
      ],
    });
    expect(out.comments).toEqual({
      count: 16,
      count_this_week: 2,
      recent: [
        // newest comment by the bot wins (302 vs 301).
        { repo: "o/a", title: "o/a#3", url: "https://github.com/o/a/pull/3#issuecomment-302", at: recentA },
        { repo: "o/b", title: "o/b#8", url: "https://github.com/o/b/issues/8#issuecomment-401", at: recentB },
      ],
    });
    expect(out.generated_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it("spends 4 Search requests regardless of consumer count — Search allows 30/minute", async () => {
    const { __test } = await import("../src/index");
    // The bound is the reason the queries are combined: one request per bot
    // per bucket crossed 30/minute at 8 consumers, and because the fallback
    // TTL is 30s a structurally oversized refresh re-attempts forever instead
    // of draining. 20 consumers here — 4·N would be 80.
    const consumers = Array.from({ length: 20 }, (_, i) => ({
      repo: `o/r${i}`,
      bot_name: `bot-${i}`,
    }));
    let searchCalls = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/data/consumers.json")) {
        return new Response(JSON.stringify(consumers), { status: 200 });
      }
      if (url.includes("/search/issues")) {
        searchCalls++;
        return new Response(JSON.stringify({ total_count: 0, items: [] }), {
          status: 200,
        });
      }
      throw new Error(`unexpected fetch ${url}`);
    }) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL:
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
    };
    await __test.refreshActivity(env);
    expect(searchCalls).toBe(4);
  });

  it("falls back to the parent URL when the follow-up fetch fails", async () => {
    const { __test } = await import("../src/index");
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/data/consumers.json")) {
        return new Response(
          JSON.stringify([{ repo: "o/r", bot_name: "bot-a" }]),
          { status: 200 },
        );
      }
      if (url.includes("/search/issues")) {
        // `commenter:...-reviewed-by:...` contains both keywords; match
        // `commenter` first so it doesn't fall through to the reviews case.
        if (url.includes("commenter")) {
          return new Response(
            JSON.stringify({
              total_count: 1,
              items: [
                {
                  html_url: "https://github.com/o/r/issues/6",
                  title: "Issue 6",
                  updated_at: "2026-05-10T00:00:00Z",
                  repository_url: "https://api.github.com/repos/o/r",
                  number: 6,
                },
              ],
            }),
            { status: 200 },
          );
        }
        if (url.includes("reviewed-by")) {
          return new Response(
            JSON.stringify({
              total_count: 1,
              items: [
                {
                  html_url: "https://github.com/o/r/pull/5",
                  title: "PR 5",
                  updated_at: "2026-05-10T00:00:00Z",
                  repository_url: "https://api.github.com/repos/o/r",
                  number: 5,
                },
              ],
            }),
            { status: 200 },
          );
        }
        return new Response(JSON.stringify({ total_count: 0, items: [] }), { status: 200 });
      }
      // Follow-up review-comment lookup 404s; follow-up comment lookup returns no bot match.
      if (url.includes("/pulls/5/comments")) {
        return new Response("not found", { status: 404 });
      }
      if (url.includes("/issues/6/comments")) {
        return new Response(
          JSON.stringify([
            { user: { login: "someone-else" }, html_url: "x", created_at: "2026-05-10T00:00:00Z" },
          ]),
          { status: 200 },
        );
      }
      throw new Error(`unexpected fetch ${url}`);
    }) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL:
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
    };
    const out = await __test.refreshActivity(env);
    // Both buckets fall back to the parent URL rather than dropping the row.
    expect(out.reviews.recent).toEqual([
      { repo: "o/r", title: "PR 5", url: "https://github.com/o/r/pull/5", at: "2026-05-10T00:00:00Z" },
    ]);
    expect(out.comments.recent).toEqual([
      { repo: "o/r", title: "Issue 6", url: "https://github.com/o/r/issues/6", at: "2026-05-10T00:00:00Z" },
    ]);
  });

  it("throws when a Search query fails (rate-limit / transient) — sinks the refresh rather than caching all-zero", async () => {
    const { __test } = await import("../src/index");
    // One bucket's query is rate-limited. Swallowing it to an empty bucket used
    // to cache an all-zero "success" at the full TTL, so the site rendered
    // "0 PRs / …" for up to the stale-serve window. The refresh must throw so
    // coalesceAndRefresh keeps the last good entry (warm) and the cold path
    // short-fallbacks and retries.
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/data/consumers.json")) {
        return new Response(
          JSON.stringify([{ repo: "o/r", bot_name: "bot-a" }]),
          { status: 200 },
        );
      }
      if (url.includes("commenter")) {
        return new Response("rate limited", { status: 429 });
      }
      return new Response(JSON.stringify({ total_count: 1, items: [] }), {
        status: 200,
      });
    }) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL:
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
    };
    await expect(__test.refreshActivity(env)).rejects.toThrow(
      /search request failed \(429\)/,
    );
  });

  it("returns empty buckets when there are no consumers", async () => {
    const { __test } = await import("../src/index");
    globalThis.fetch = makeFetch(
      new Map([[
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
        [],
      ]]),
    ) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL:
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
    };
    const out = await __test.refreshActivity(env);
    expect(out.prs).toEqual({ count: 0, count_this_week: 0, recent: [] });
    expect(out.issues).toEqual({ count: 0, count_this_week: 0, recent: [] });
    expect(out.reviews).toEqual({ count: 0, count_this_week: 0, recent: [] });
    expect(out.comments).toEqual({ count: 0, count_this_week: 0, recent: [] });
  });

  it("throws on a 401 from Search — surfaces so the refresh falls back", async () => {
    const { __test } = await import("../src/index");
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/data/consumers.json")) {
        return new Response(
          JSON.stringify([{ repo: "o/r", bot_name: "bot-a" }]),
          { status: 200 },
        );
      }
      return new Response("bad credentials", { status: 401 });
    }) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL:
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
    };
    await expect(__test.refreshActivity(env)).rejects.toThrow(
      /search request failed \(401\)/,
    );
  });

  it("keeps only the newest RECENT_PER_BUCKET items per bucket", async () => {
    const { __test } = await import("../src/index");
    const items = Array.from({ length: 15 }, (_, i) => ({
      html_url: `https://github.com/o/r/pull/${i}`,
      title: `#${i}`,
      // i=0 is newest (largest timestamp), i=14 is oldest
      updated_at: new Date(2026, 0, 1, 0, 15 - i).toISOString(),
      repository_url: "https://api.github.com/repos/o/r",
    }));
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/data/consumers.json")) {
        return new Response(
          JSON.stringify([{ repo: "o/r", bot_name: "bot-a" }]),
          { status: 200 },
        );
      }
      const body = url.includes("is%3Apr") ? { total_count: 15, items } : { total_count: 0, items: [] };
      return new Response(JSON.stringify(body), { status: 200 });
    }) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL:
        "https://raw.githubusercontent.com/max-sixty/tend/main/data/consumers.json",
    };
    const out = await __test.refreshActivity(env);
    expect(out.prs.count).toBe(15);
    expect(out.prs.recent).toHaveLength(10);
    expect(out.prs.recent.map((r) => r.title)).toEqual(
      ["#0", "#1", "#2", "#3", "#4", "#5", "#6", "#7", "#8", "#9"], // newest first
    );
  });
});

describe("isConsumerArray (shape validation)", () => {
  it("accepts valid consumers", async () => {
    const { __test } = await import("../src/index");
    expect(
      __test.isConsumerArray([{ repo: "max-sixty/tend", bot_name: "tend-agent" }]),
    ).toBe(true);
  });

  it("rejects path-traversal repo values", async () => {
    const { __test } = await import("../src/index");
    for (const bad of [
      "../etc/passwd",
      "../foo/bar",
      "foo/..",
      "../..",
      "./b",
      "a/..b",
      "a/b..",
      ".hidden/repo",
      "-leading/repo",
      "a/-leading",
      "a/b/c",
      "no-slash",
    ]) {
      expect(
        __test.isConsumerArray([{ repo: bad, bot_name: "x" }]),
        `should reject ${bad}`,
      ).toBe(false);
    }
  });

  it("isValidRepo accepts realistic GitHub repos", async () => {
    const { __test } = await import("../src/index");
    for (const good of [
      "max-sixty/tend",
      "PRQL/prql",
      "max-sixty/cargo-affected",
      "numbagg/numbagg",
      "a/b",
      "org_with_underscore/repo.with.dots",
    ]) {
      expect(__test.isValidRepo(good), `should accept ${good}`).toBe(true);
    }
  });

  it("rejects bot names that would break Search query syntax", async () => {
    const { __test } = await import("../src/index");
    for (const bad of ["", "bot space", "bot/slash", "bot:colon", "-leading", "."]) {
      expect(__test.isValidBotName(bad), `should reject ${bad}`).toBe(false);
    }
    for (const good of ["tend-agent", "bot_1", "PRQL-bot", "a"]) {
      expect(__test.isValidBotName(good), `should accept ${good}`).toBe(true);
    }
  });

  it("rejects non-arrays and malformed entries", async () => {
    const { __test } = await import("../src/index");
    expect(__test.isConsumerArray({ repo: "o/r" })).toBe(false);
    expect(__test.isConsumerArray([{ not_repo: "x" }])).toBe(false);
    expect(__test.isConsumerArray([null])).toBe(false);
    expect(__test.isConsumerArray(null)).toBe(false);
  });
});

describe("getConsumers", () => {
  it("rejects malformed JSON shape (does not poison KV)", async () => {
    const { __test } = await import("../src/index");
    const kv = makeFakeKv();
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify([{ not_repo: "x" }]), { status: 200 }),
    ) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: kv,
      ALLOWED_ORIGIN: "*",
      REPOS_URL: "https://example.test/consumers.json",
    };
    await expect(__test.getConsumers(env)).rejects.toThrow(/shape validation/);
    expect(await kv.get("repos:v1")).toBeNull();
  });

  it("rejects non-array body", async () => {
    const { __test } = await import("../src/index");
    globalThis.fetch = vi.fn(
      async () => new Response(JSON.stringify({ repo: "o/r" }), { status: 200 }),
    ) as unknown as typeof fetch;

    const env = {
      GITHUB_TOKEN: "tok",
      CACHE: makeFakeKv(),
      ALLOWED_ORIGIN: "*",
      REPOS_URL: "https://example.test/consumers.json",
    };
    await expect(__test.getConsumers(env)).rejects.toThrow();
  });
});

describe("coalesceAndRefresh (background refresh on stale-hit)", () => {
  it("keeps the previous cached entry alive when the refresh throws (does not overwrite with empty)", async () => {
    const { __test } = await import("../src/index");
    const { store, api } = fakeCache();
    (globalThis as unknown as { caches: CacheStorage }).caches = {
      default: api,
    } as unknown as CacheStorage;

    const cacheKey = new Request("https://api.example/activity");
    const goodBody = JSON.stringify({ prs: { count: 994 } });
    const stale = new Response(goodBody, {
      headers: { "Content-Type": "application/json" },
    });
    store.set(cacheKey.url, stale);

    const env = { ALLOWED_ORIGIN: "*" } as unknown as Parameters<typeof __test.coalesceAndRefresh>[2];
    await __test.coalesceAndRefresh(cacheKey, stale, env, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      refresh: async () => {
        throw new Error("github down");
      },
    });

    const entry = store.get(cacheKey.url);
    expect(entry).toBeDefined();
    expect(await entry!.text()).toBe(goodBody); // unchanged — empty payload did NOT win
  });

  it("overwrites the cached entry with the fresh response on a successful refresh", async () => {
    const { __test } = await import("../src/index");
    const { store, api } = fakeCache();
    (globalThis as unknown as { caches: CacheStorage }).caches = {
      default: api,
    } as unknown as CacheStorage;

    const cacheKey = new Request("https://api.example/activity");
    const stale = new Response(JSON.stringify({ prs: { count: 1 } }), {
      headers: { "Content-Type": "application/json" },
    });
    store.set(cacheKey.url, stale);

    const env = { ALLOWED_ORIGIN: "*" } as unknown as Parameters<typeof __test.coalesceAndRefresh>[2];
    await __test.coalesceAndRefresh(cacheKey, stale, env, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      refresh: async () => ({ prs: { count: 999 } }),
    });

    const entry = store.get(cacheKey.url);
    const body = JSON.parse(await entry!.text()) as { prs: { count: number } };
    expect(body.prs.count).toBe(999);
  });

  it("pulls a sibling colo's fresh KV entry instead of fanning out (kvKey)", async () => {
    const { __test } = await import("../src/index");
    const { store, api } = fakeCache();
    (globalThis as unknown as { caches: CacheStorage }).caches = {
      default: api,
    } as unknown as CacheStorage;

    const kv = makeFakeKv();
    const staleAt = Date.now() + 100_000; // fresh, but not a full budget out
    await kv.put(
      "activity:v1",
      JSON.stringify({ payload: { prs: { count: 994 } }, staleAt }),
    );
    const cacheKey = new Request("https://api.example/activity");
    const stale = new Response(JSON.stringify({ prs: { count: 1 } }), {
      headers: { "Content-Type": "application/json" },
    });
    store.set(cacheKey.url, stale);

    const env = { ALLOWED_ORIGIN: "*", CACHE: kv } as unknown as Parameters<
      typeof __test.coalesceAndRefresh
    >[2];
    const refresh = vi.fn(async () => {
      throw new Error("should not fan out — KV is fresh");
    });
    await __test.coalesceAndRefresh(cacheKey, stale, env, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      kvKey: "activity:v1",
      refresh,
    });

    expect(refresh).not.toHaveBeenCalled();
    const entry = store.get(cacheKey.url);
    expect(JSON.parse(await entry!.text())).toEqual({ prs: { count: 994 } });
    // The colo entry inherits KV's stale-at, not now + ttl.ok — otherwise this
    // common warm-stale path would reset the shared clock and drift freshness.
    expect(entry?.headers.get(__test.STALE_AT_HEADER)).toBe(String(staleAt));
  });
});

describe("refreshShared (KV-coordinated refresh)", () => {
  it("reuses a fresh KV entry, returning its original stale-at (not a re-minted one)", async () => {
    const { __test } = await import("../src/index");
    const kv = makeFakeKv();
    // staleAt 100s out — NOT a full budget (300s). The reused entry must carry
    // this instant forward so callers don't reset the shared clock.
    const staleAt = Date.now() + 100_000;
    await kv.put("activity:v1", JSON.stringify({ payload: { prs: { count: 994 } }, staleAt }));
    const env = { CACHE: kv } as unknown as Parameters<typeof __test.refreshShared>[0];
    const refresh = vi.fn(async () => {
      throw new Error("should not fan out — KV is fresh");
    });

    const result = await __test.refreshShared(env, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      kvKey: "activity:v1",
      refresh,
    });

    expect(result.payload).toEqual({ prs: { count: 994 } });
    expect(result.staleAt).toBe(staleAt); // inherited, not now + ttl.ok
    expect(refresh).not.toHaveBeenCalled();
  });

  it("fans out and publishes the result to KV when no entry exists", async () => {
    const { __test } = await import("../src/index");
    const kv = makeFakeKv();
    const env = { CACHE: kv } as unknown as Parameters<typeof __test.refreshShared>[0];
    const refresh = vi.fn(async () => ({ prs: { count: 5 } }));

    const result = await __test.refreshShared(env, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      kvKey: "activity:v1",
      refresh,
    });

    expect(result.payload).toEqual({ prs: { count: 5 } });
    expect(result.staleAt).toBeGreaterThan(Date.now()); // freshly minted
    expect(refresh).toHaveBeenCalledOnce();
    const stored = (await kv.get("activity:v1", "json")) as {
      payload: unknown;
      staleAt: number;
    };
    expect(stored).toEqual(result); // published verbatim for siblings
  });

  it("fans out a stale KV entry rather than serving it", async () => {
    const { __test } = await import("../src/index");
    const kv = makeFakeKv();
    await kv.put(
      "activity:v1",
      JSON.stringify({ payload: { prs: { count: 1 } }, staleAt: Date.now() - 1000 }),
    );
    const env = { CACHE: kv } as unknown as Parameters<typeof __test.refreshShared>[0];
    const refresh = vi.fn(async () => ({ prs: { count: 999 } }));

    const result = await __test.refreshShared(env, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      kvKey: "activity:v1",
      refresh,
    });

    expect(result.payload).toEqual({ prs: { count: 999 } });
    expect(result.staleAt).toBeGreaterThan(Date.now()); // re-minted, not the stale instant
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("fans out without touching KV when no kvKey is set (currently-tending)", async () => {
    const { __test } = await import("../src/index");
    const kv = makeFakeKv();
    const env = { CACHE: kv } as unknown as Parameters<typeof __test.refreshShared>[0];
    const refresh = vi.fn(async () => ({ currently_tending: [] }));

    const result = await __test.refreshShared(env, {
      cacheKeyPath: "/currently-tending",
      ttl: { ok: 30, fallback: 5 },
      refresh,
    });

    expect(result.payload).toEqual({ currently_tending: [] });
    expect(result.staleAt).toBeGreaterThan(Date.now());
    expect(refresh).toHaveBeenCalledOnce();
    expect(await kv.get("activity:v1")).toBeNull();
  });
});

describe("refreshAndCache (cold cache)", () => {
  function fakeCtx() {
    const tasks: Promise<unknown>[] = [];
    return {
      tasks,
      ctx: {
        waitUntil: (p: Promise<unknown>) => tasks.push(p),
      } as unknown as ExecutionContext,
    };
  }

  function installCache() {
    const fc = fakeCache();
    (globalThis as unknown as { caches: CacheStorage }).caches = {
      default: fc.api,
    } as unknown as CacheStorage;
    return fc;
  }

  it("caches and returns the fresh 200 on a successful cold refresh", async () => {
    const { __test } = await import("../src/index");
    const { store } = installCache();
    const { tasks, ctx } = fakeCtx();
    const cacheKey = new Request("https://api.example/activity");
    const env = { ALLOWED_ORIGIN: "*" } as unknown as Parameters<
      typeof __test.refreshAndCache
    >[1];

    const resp = await __test.refreshAndCache(cacheKey, env, ctx, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      refresh: async () => ({ prs: { count: 5 } }),
    });

    expect(resp.status).toBe(200);
    expect(JSON.parse(await resp.clone().text())).toEqual({ prs: { count: 5 } });
    await Promise.all(tasks);
    expect(store.get(cacheKey.url)?.status).toBe(200);
  });

  it("returns a 503 (not a 200 all-zero payload) when the cold refresh throws, and negative-caches it", async () => {
    const { __test } = await import("../src/index");
    const { store } = installCache();
    const { tasks, ctx } = fakeCtx();
    const cacheKey = new Request("https://api.example/activity");
    const env = { ALLOWED_ORIGIN: "*" } as unknown as Parameters<
      typeof __test.refreshAndCache
    >[1];

    const resp = await __test.refreshAndCache(cacheKey, env, ctx, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      refresh: async () => {
        throw new Error("github down");
      },
    });

    // Non-OK so the site's fetchJson returns null and hides the section
    // rather than rendering fabricated zeros from a 200 all-zero body.
    expect(resp.ok).toBe(false);
    expect(resp.status).toBe(503);
    await Promise.all(tasks);
    expect(store.get(cacheKey.url)?.status).toBe(503); // negative-cached
  });

  it("serves a sibling colo's fresh KV entry on a cold colo cache, no fanout, inheriting its stale-at (kvKey)", async () => {
    const { __test } = await import("../src/index");
    const { store } = installCache();
    const { tasks, ctx } = fakeCtx();
    const kv = makeFakeKv();
    // KV entry written earlier: it goes stale 100s from now, NOT a full budget
    // (300s) from now. The colo entry must inherit this instant, otherwise the
    // colo resets the shared clock and serves data up to a budget too long.
    const staleAt = Date.now() + 100_000;
    await kv.put(
      "activity:v1",
      JSON.stringify({ payload: { prs: { count: 994 } }, staleAt }),
    );
    const cacheKey = new Request("https://api.example/activity");
    const env = { ALLOWED_ORIGIN: "*", CACHE: kv } as unknown as Parameters<
      typeof __test.refreshAndCache
    >[1];
    const refresh = vi.fn(async () => {
      throw new Error("should not fan out — KV is fresh");
    });

    const resp = await __test.refreshAndCache(cacheKey, env, ctx, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      kvKey: "activity:v1",
      refresh,
    });

    expect(resp.status).toBe(200);
    expect(JSON.parse(await resp.clone().text())).toEqual({ prs: { count: 994 } });
    expect(resp.headers.get(__test.STALE_AT_HEADER)).toBe(String(staleAt));
    expect(refresh).not.toHaveBeenCalled();
    await Promise.all(tasks);
    const colo = store.get(cacheKey.url);
    expect(colo?.status).toBe(200); // colo hydrated from KV
    expect(colo?.headers.get(__test.STALE_AT_HEADER)).toBe(String(staleAt)); // not now+ttl.ok
  });

  it("serves a stale KV entry immediately and revalidates in the background (kvKey)", async () => {
    const { __test } = await import("../src/index");
    const { store } = installCache();
    const { tasks, ctx } = fakeCtx();
    const kv = makeFakeKv();
    await kv.put(
      "activity:v1",
      JSON.stringify({ payload: { prs: { count: 1 } }, staleAt: Date.now() - 1000 }),
    );
    const cacheKey = new Request("https://api.example/activity");
    const env = { ALLOWED_ORIGIN: "*", CACHE: kv } as unknown as Parameters<
      typeof __test.refreshAndCache
    >[1];
    const refresh = vi.fn(async () => ({ prs: { count: 999 } }));

    const resp = await __test.refreshAndCache(cacheKey, env, ctx, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      kvKey: "activity:v1",
      refresh,
    });

    // Served immediately = the stale payload (no waiting on the fanout).
    expect(JSON.parse(await resp.clone().text())).toEqual({ prs: { count: 1 } });
    await Promise.all(tasks); // run the background revalidation
    expect(refresh).toHaveBeenCalledOnce();
    // KV and colo cache now hold the fresh payload for the next viewers.
    const stored = (await kv.get("activity:v1", "json")) as { payload: unknown };
    expect(stored.payload).toEqual({ prs: { count: 999 } });
    expect(JSON.parse(await store.get(cacheKey.url)!.text())).toEqual({ prs: { count: 999 } });
  });

  it("503s and publishes nothing when colo cache and KV are both empty and the fanout fails (kvKey)", async () => {
    const { __test } = await import("../src/index");
    const { store } = installCache();
    const { tasks, ctx } = fakeCtx();
    const kv = makeFakeKv();
    const cacheKey = new Request("https://api.example/activity");
    const env = { ALLOWED_ORIGIN: "*", CACHE: kv } as unknown as Parameters<
      typeof __test.refreshAndCache
    >[1];

    const resp = await __test.refreshAndCache(cacheKey, env, ctx, {
      cacheKeyPath: "/activity",
      ttl: { ok: 300, fallback: 30 },
      kvKey: "activity:v1",
      refresh: async () => {
        throw new Error("github down");
      },
    });

    expect(resp.status).toBe(503);
    await Promise.all(tasks);
    expect(store.get(cacheKey.url)?.status).toBe(503); // negative-cached
    expect(await kv.get("activity:v1")).toBeNull(); // nothing published on failure
  });
});

describe("isStale (stale-while-revalidate decision)", () => {
  const now = 1_700_000_000_000;

  it("treats a future stamp as fresh and past/at as stale", async () => {
    const { __test } = await import("../src/index");
    expect(__test.isStale(String(now + 1000), now)).toBe(false);
    expect(__test.isStale(String(now - 1000), now)).toBe(true);
    expect(__test.isStale(String(now), now)).toBe(true); // boundary refreshes
  });

  it("treats a missing or garbled stamp as stale", async () => {
    const { __test } = await import("../src/index");
    expect(__test.isStale(null, now)).toBe(true);
    expect(__test.isStale("", now)).toBe(true);
    expect(__test.isStale("not-a-number", now)).toBe(true);
  });
});

// Fake the colo cache so tests can observe what gets written on success vs
// failure. caches.default is Cloudflare-specific; in node it's undefined.
function fakeCache() {
  const store = new Map<string, Response>();
  return {
    store,
    api: {
      async match(req: Request) {
        return store.get(req.url)?.clone();
      },
      async put(req: Request, resp: Response) {
        store.set(req.url, resp.clone());
      },
    } as unknown as Cache,
  };
}

function makeFakeKv(): KVNamespace {
  const store = new Map<string, string>();
  return {
    async get(key: string, type?: string) {
      const v = store.get(key);
      if (v === undefined) return null;
      return type === "json" ? JSON.parse(v) : v;
    },
    async put(key: string, value: string) {
      store.set(key, value);
    },
    async delete(key: string) {
      store.delete(key);
    },
    async list() {
      return { keys: [], list_complete: true as const, cacheStatus: null };
    },
    async getWithMetadata() {
      return { value: null, metadata: null, cacheStatus: null };
    },
  } as unknown as KVNamespace;
}
