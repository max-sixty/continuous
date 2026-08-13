# tend site

The tend marketing site (<https://tend-src.com>), built with Astro and
deployed to GitHub Pages via `.github/workflows/publish-site.yaml`.

## Run locally

In a `wt` worktree the dev server is already running: a post-start hook in
[`.config/wt.toml`](../.config/wt.toml) installs deps and starts Astro on a
port derived from the branch name. Don't run `npm run dev` — it starts a
second server on a different port instead of reusing the one listening.

```sh
wt list statusline --format json | jq -r '.[].url'   # the URL to open
wt config state logs                                 # dev-server output
```

Outside a `wt` worktree — a plain clone, or CI — start it by hand and open
<http://localhost:4321/>:

```sh
cd site
npm install
npm run dev
```

Either way, the live-data elements (currently-tending indicator, stats strip,
activity feed) fetch the Worker at `api.tend-src.com`; set `PUBLIC_WORKER_URL`
in `.env.local` to point elsewhere (see `.env.example`).

## Build

```sh
npm run build      # writes static files to site/dist/
npm run preview    # serve the built site
```

## What's here

- `src/pages/index.astro` — the single-page site (hero + 6 areas + quick start + security + footer)
- `src/components/Logo.astro` — animated SVG of the tend mark: a pen traces the outline, the colour floods in behind it, then it settles with a faint breath; pass `static` for the header lockup
- `src/layouts/Base.astro` — page shell, header, footer, font preconnect
- `src/styles/global.css` — palette, typography, marginalia grid, all layout
- `src/components/CurrentlyTending.astro`, `src/components/Stats.astro`, `src/components/Activity.astro` — runtime-fetch the live-data Worker; hidden when empty or the fetch fails
- `src/lib/api.ts` — live-data Worker client: `fetchJson` + `liveData` (the fetch/render/reveal cycle the three components share); base URL overridable via `PUBLIC_WORKER_URL`
- `src/lib/time.ts` — compact relative-time formatter shared by the live-data components
- `public/logo.png` — copied from `../assets/`
- `public/favicon.svg` — transparent SVG favicon, mark path mirrored from `Logo.astro`; `prefers-color-scheme: dark` switches the oak fill to the lighter dark-mode shade
