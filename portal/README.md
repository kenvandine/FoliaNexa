# FoliaNexa player hub (`portal/`)

The public-facing half of the VPS edge (PLAN.md §7A) — leaderboards,
player profiles, and a "recently active" pulse. Consumes mgmt's
unauthenticated `GET /api/v1/public/*` routes
(`mgmt/src/folia_mgmt/routers/public_stats.py`), which are in turn fed by
the in-house stats plugin (catalog id `FoliaNexaStats`).

## Why no build step

Plain HTML/CSS/JS, same hand-written style as mgmt's own dashboard
(`mgmt/src/folia_mgmt/static/index.html`) — this repo has zero Node.js
build tooling precedent anywhere, and a leaderboard/profile page doesn't
need one. `portal.js` holds the shared fetch/formatting helpers; each page
is otherwise self-contained.

## Deploying

No `snapcraft.yaml` — there's no daemon/process here, just static files, so
a snap would be pure overhead. Deploy with
[`deploy/vps/deploy-portal.sh`](../deploy/vps/deploy-portal.sh), which
rsyncs this directory to wherever the VPS's Caddyfile points
`PORTAL_ROOT` (default `/srv/folianexa-portal`):

```bash
./deploy/vps/deploy-portal.sh --vps-host root@your-vps-ip
```

## Configuring which API it talks to

Each page defaults to guessing the public stats API's origin by swapping
this page's `play.` hostname prefix for `api.` (matching
`deploy/vps/Caddyfile`'s default subdomain scheme — see
`portal.js`'s `resolveApiBase()`). If you used a different subdomain
scheme, uncomment and set the `<meta name="api-base" content="...">` tag
at the top of each HTML file instead.

## Local testing

```bash
python3 -m http.server 8000
```

then open `http://localhost:8000/?` — but note the API-base guess above
won't resolve to anything real unless you also set the `<meta
name="api-base">` tag to point at a real (or locally-running) mgmt
instance's `/api/v1/public` prefix. mgmt's CORS handling for
`/api/v1/public/*` (`mgmt/src/folia_mgmt/main.py`) allows this — it's not
restricted to the production `play.<domain>` origin.

## What's real vs. unverified

All three pages were exercised end-to-end in this environment: a real
`folia-nexa-mgmt` instance running (`folia-nexa-mgmt serve`), seeded with
real requests through `POST /api/v1/stats/report`, and all three pages
loaded in a real headless Chromium against it (screenshots checked by
hand) — confirming the recently-active/all-players grids, the leaderboard
sort order, the player profile's stat tiles, and the playtime heatmap all
render correctly from real API responses, not just that the JSON contracts
match (which `mgmt/tests/test_public_stats.py` also covers). Not verified:
Crafatar's avatar CDN actually rendering — this sandbox has no route to
the public internet, so avatar `<img>` tags render blank here; the
`<img src>` URLs themselves are correct and this is expected to work
wherever this is actually deployed with real internet access.
