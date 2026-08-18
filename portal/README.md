# FoliaNexa player hub (`portal/`)

The public-facing half of the VPS edge (PLAN.md §7A) — leaderboards,
player profiles, a "recently active" pulse, and a per-world "who's
online" view (`online.html`). The leaderboards/profiles/pulse pages are
fed by the in-house stats plugin (catalog id `FoliaNexaStats`) via `POST
/api/v1/stats/report`; `online.html` is fed by `folia-routes-sync` (the
Velocity proxy plugin) via `POST /api/v1/presence/report`
(`mgmt/src/folia_mgmt/routers/presence.py`) — Velocity's own
`RegisteredServer.getPlayersConnected()`, reported on its existing ~5s
poll cycle, so it's closer to real presence than the stats-report-recency
proxy the pulse card uses. All of it reads through mgmt's unauthenticated
`GET /api/v1/public/*` routes (`mgmt/src/folia_mgmt/routers/
public_stats.py`).

## Why no build step

Plain HTML/CSS/JS, same hand-written style as mgmt's own dashboard
(`mgmt/src/folia_mgmt/static/index.html`) — this repo has zero Node.js
build tooling precedent anywhere, and a leaderboard/profile page doesn't
need one. `portal.js` holds the shared fetch/formatting helpers; each page
is otherwise self-contained.

## Deploying

Packaged as its own snap, `folia-nexa-portal`, like every other component
in this cluster — `src/folia_portal/serve.py` is a small dependency-free
`http.server` wrapper that serves this directory's `*.html`/`portal.css`/
`portal.js` (bundled into the snap at build time, see `snapcraft.yaml`'s
`override-build`) on a local port (default `127.0.0.1:8090`, override with
`snap set folia-nexa-portal listen-port=<port>` / `listen-host=<host>`).
No server-side logic beyond "serve these static files" — all the real data
still comes from mgmt's public API, fetched client-side by `portal.js`.

```bash
cd portal && snapcraft
sudo snap install ./folia-nexa-portal_0.1_amd64.snap --dangerous
sudo snap start folia-nexa-portal.daemon
```

On the VPS, Caddy reverse-proxies `play.<domain>` to this daemon's local
port (`deploy/vps/Caddyfile`'s `PORTAL_UPSTREAM`, default matching
`listen-port`'s own default) the same way it already does for
`admin.<domain>`/`api.<domain>` — see `docs/vps-edge-deployment.md`'s
Phase 6. There's no separate rsync step anymore (the old
`deploy/vps/deploy-portal.sh` is gone) — updating the portal is rebuild +
reinstall, same as any other snap in this repo.

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
match (which `mgmt/tests/test_public_stats.py` also covers). Avatars are
now self-hosted (`GET /api/v1/public/players/{uuid}/avatar`,
`mgmt/src/folia_mgmt/avatar.py`) rather than pointing at crafatar.com — a
2026-08-16 outage there (Cloudflare 521 on every UUID, confirmed via
direct testing, not something specific to any one player) broke every
avatar on the portal at once, since it was the one piece of this page
depending on a third party outside this project's control. mgmt now
fetches the player's real skin from Mojang's session server and renders
the face itself with Pillow, falling back to a flat placeholder image if
Mojang can't be reached rather than ever serving a broken `<img>`.

`online.html` and its backing pieces (`WorldPresence` model, `POST
/api/v1/presence/report`, `GET /api/v1/public/worlds`,
`FoliaRoutesSyncPlugin`'s new presence-reporting job) are covered by real
unit/integration tests — `mgmt/tests/test_presence.py`,
`mgmt/tests/test_public_stats.py`'s `test_worlds_online_*` cases (full
mgmt API round trip: enroll a host, place a world, report presence, read
it back), and `proxy`'s `PresenceJsonTest` (real Gson JSON building) — all
passing. `online.html` itself was also exercised the same way as the
other three pages: a real `folia-nexa-mgmt` instance running, seeded via
real `POST /api/v1/presence/report` calls against real Mojang UUIDs
(Notch, jeb_), loaded in real headless Chromium — confirming the world
cards, player-count badges, avatars, and the empty-world/stale-presence
states all render correctly (a presence report older than
`public_presence_stale_seconds` correctly fell back to "0 online" in a
live run, not just in the pytest suite). Not yet verified: a real
Velocity proxy actually calling `RegisteredServer.getPlayersConnected()`
and having that reach a running mgmt instance end-to-end — that needs a
live cluster, which wasn't available in this environment (the check above
posted directly to `POST /api/v1/presence/report`, standing in for what
`FoliaRoutesSyncPlugin` would send).

`folia-nexa-portal`'s `snapcraft.yaml` — confirmed to build for real with
`snapcraft` 9.0.1 against a real LXD-based build backend, same as every
other component in this repo: a real venv built from `pyproject.toml`,
`scripts/run-portal-daemon.sh` installed to `bin/`, and the four static
pages plus `portal.css`/`portal.js` copied into the snap's own `portal/`
directory by the part's `override-build` — verified by unsquashing the
resulting `.snap` and listing its contents, not just by the build
succeeding. `src/folia_portal/serve.py` itself has a real pytest suite
(`tests/test_serve.py`) that starts the actual `ThreadingHTTPServer` on a
real socket and confirms it serves files, 404s on a missing path, and
can't escape its static root. Not verified: actually *installing*
(`snap install --dangerous`) or starting the snap — no root/interactive-
sudo access was available in this environment, same limitation as every
other snap in this repo (see `CLAUDE.md`'s own note on this).
