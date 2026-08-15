# VPS Edge Deployment: WireGuard Tunnel + Public Portal

This is a task-oriented walkthrough for standing up the public edge
described in PLAN.md §7A: a Linode (or any) VPS that tunnels into your
home network over WireGuard, so players and the public reach the cluster
through the VPS with **zero inbound port forwarding at home**. For the
*why* behind this shape, see PLAN.md §7A; for the base cluster this
extends (mgmt, hosts, worlds, the proxy), see `CLAUDE.md` and PLAN.md §1–§9.

This doc assumes you already have a working cluster at home per
`CLAUDE.md`'s bootstrap phases — a running `folia-nexa-mgmt`, at least one
trusted LXD host, and worlds you can already reach on your home LAN. What
follows adds a public front door in front of that, it doesn't replace any
of it. **`folia-nexa-mgmt` itself never moves** — it stays on your home
network for the whole of this guide.

## Overview

```
                    Public Internet
                          │
                          ▼
                  ┌───────────────┐
                  │   Linode VPS   │
                  │  Caddy (TLS)   │──── admin.<domain>  ──┐
                  │  folia-nexa-   │──── api.<domain>    ──┤
                  │    proxy       │──── play.<domain>   ──┤ (portal/,
                  │  (Velocity)    │                       │  static)
                  └───────┬────────┘                       │
                          │ WireGuard (UDP 51820,           │
                          │ dialed FROM home, no             │
                          │ inbound port forward)            │
                          ▼                                  │
                  ┌───────────────┐                          │
                  │  Home network │◄─────────────────────────┘
                  │  folia-nexa-  │
                  │    mgmt       │
                  │  LXD hosts /  │
                  │  worlds       │
                  └───────────────┘
```

Three things move to (or originate from) the VPS: `folia-nexa-proxy`
(Minecraft's public port), Caddy (TLS termination + the admin/public API
reverse proxy), and the static `portal/` player hub. Everything else —
mgmt, the LXD hosts, the worlds themselves — stays exactly where it is
today.

## Phase 1: Provision the VPS

Any Linode plan reachable on a public IPv4 address works; nothing here is
Linode-specific beyond "a VPS with a public IP and outbound package
access." Ubuntu 24.04 to match the rest of this cluster's target OS.

```bash
sudo apt update
sudo apt install -y wireguard-tools caddy
sudo ufw allow 51820/udp   # WireGuard
sudo ufw allow 80/tcp 443/tcp   # Caddy / Let's Encrypt
```

## Phase 2: DNS

Point three DNS records at the VPS's public IP (matching the confirmed
subdomain scheme — adjust if you chose differently):

| Record | Purpose |
| --- | --- |
| `admin.<domain>` | mgmt dashboard/API, reverse-proxied |
| `api.<domain>` | public leaderboard/stats API |
| `play.<domain>` | the static player hub (`portal/`) |

(Minecraft's own hostname, e.g. `play.<domain>` or a separate `mc.<domain>`
SRV record pointed at the VPS's `:25565`, is up to you — it isn't an HTTP
subdomain and Caddy never touches it.)

## Phase 3: WireGuard tunnel

Run [`deploy/vps/setup-wireguard.sh`](../deploy/vps/setup-wireguard.sh) on
**both** ends — the VPS, and the same home box that already runs LXD
(no new hardware needed). See that script's own `--help` and
[`deploy/vps/README.md`](../deploy/vps/README.md) for the full two-pass
key-exchange flow; in short:

```bash
# On the VPS:
sudo ./deploy/vps/setup-wireguard.sh --role vps
# -> prints the VPS's public key

# On the home LXD host:
sudo ./deploy/vps/setup-wireguard.sh --role home \
  --peer-public-key <vps-pubkey-from-above> \
  --peer-endpoint <vps-public-ip>:51820
# -> writes home's full config, prints home's own public key

# Back on the VPS, finish the exchange:
sudo ./deploy/vps/setup-wireguard.sh --role vps \
  --peer-public-key <home-pubkey-from-above>

# On both:
sudo systemctl enable --now wg-quick@wg0
```

The home side always dials out to the VPS and keeps the NAT mapping alive
(`PersistentKeepalive`) — this is the entire trick for needing no inbound
port forward at home. Once `wg0` is up on both sides:

```bash
sudo wg show   # confirm a recent handshake on both ends
ping 10.66.0.1   # from home, should reach the VPS
ping 10.66.0.2   # from the VPS, should reach home
```

**Scope what the VPS can actually reach.** The script's default
`AllowedIPs` on the VPS side only routes to the home peer's own tunnel
address — add mgmt's real LAN address and your LXD hosts' bridge subnet
once you know them (re-run the script with `--allowed-ips`, or edit
`/etc/wireguard/wg0.conf` directly and `wg syncconf`). Do **not** add the
LXD hosts' own `:8443` remote-API subnet — mgmt is the only thing that
should ever reach that, and PLAN.md already requires it stay off any
internet-adjacent path. On the home box, enable forwarding and scope it
with a firewall rule:

```bash
sudo sysctl -w net.ipv4.ip_forward=1
# then restrict what wg0 is allowed to forward into with nftables/iptables
# to exactly mgmt's address and the LXD bridge subnet — not the whole LAN.
```

## Phase 4: Relocate `folia-nexa-proxy` to the VPS

Install/run the existing `folia-nexa-proxy` snap on the VPS (same
`snapcraft`/`snap install --dangerous` flow as `CLAUDE.md` Phase 7), with
one change: point `FOLIA_MGMT_URL` at mgmt's WireGuard-reachable address
instead of a LAN-only one, e.g.:

```
FOLIA_MGMT_URL=http://10.0.1.10:8443
```

(Use mgmt's real tunnel-routed LAN address — the WireGuard overlay address
itself, `10.66.0.2`, also works if you'd rather route directly to that.)
No code changes needed — `FoliaRoutesSyncPlugin`'s polling and Velocity
backend registration work exactly as before, just over a different network
path. Its backend connections to world containers now cross the tunnel the
same way.

Confirm a real Minecraft client can join through the VPS's public IP with
**zero home port forwarding** before moving on — this is the core claim
this whole setup exists to deliver.

## Phase 5: Caddy — TLS + admin/public-API edge

Copy [`deploy/vps/Caddyfile`](../deploy/vps/Caddyfile) to the VPS, fill in
`ADMIN_DOMAIN`/`API_DOMAIN`/`PLAY_DOMAIN`/`MGMT_UPSTREAM`/`ACME_EMAIL`
(env vars or edit the file directly), then:

```bash
sudo caddy validate --config Caddyfile --adapter caddyfile
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

`admin.<domain>` reverse-proxies mgmt's dashboard/API unchanged — still
behind its existing bearer-token auth. `api.<domain>` only forwards
`/api/v1/public/*` (everything else 404s at this hostname, even though
mgmt would serve it — defense in depth on top of mgmt's own auth). Confirm
both:

```bash
curl -I https://admin.<domain>/healthz
curl https://api.<domain>/api/v1/public/players
```

## Phase 6: Deploy the player hub portal

```bash
./deploy/vps/deploy-portal.sh --vps-host root@<vps-ip>
```

rsyncs `portal/`'s static files to wherever Caddy's `PORTAL_ROOT` points
(default `/srv/folianexa-portal`). No build step, no snap — see
`portal/README.md`. Confirm:

```bash
open https://play.<domain>/
```

## Phase 7: Get real data flowing

The portal is only as useful as the data behind it. `GET
/api/v1/public/players` and `/leaderboards` will be empty until the
`FoliaNexaStats` plugin (catalog id, see `mgmt/src/folia_mgmt/catalog.yaml`)
is actually built and deployed to your worlds — the starter world configs
(`configs/worlds/create-survival.sh` and the two minigame scripts) already
declare `--plugin FoliaNexaStats`, but as of this writing that catalog
entry is a placeholder (`download_url: null`) pending the plugin's first
real release. See the catalog entry's own `notes` for where that plugin's
source lives once it exists.

## What's real vs. unverified

Verified in this repo's development (config-syntax and, where possible,
real-tool checks — see the commit that introduced this doc for the exact
commands run):

- `deploy/vps/Caddyfile` — validated with a real `caddy validate` (Caddy
  2.6.2), not just eyeballed.
- `deploy/vps/setup-wireguard.sh` — run end-to-end against real
  `wireguard-tools` (both the "vps" and "home" roles, across the two-pass
  key exchange), producing configs that `wg-quick strip` parses without
  error. Not run against two real machines actually establishing a live
  UDP handshake across a real NAT — that needs your real VPS and home
  network.
- `mgmt/src/folia_mgmt/routers/stats.py` and `public_stats.py` — real
  pytest suite (`mgmt/tests/test_stats.py`, `test_public_stats.py`),
  and `portal/`'s three pages were loaded in a real headless browser
  against a real running `folia-nexa-mgmt` instance seeded with real
  data through the actual ingestion API — leaderboard sorting, the
  player-profile stat tiles, and the playtime heatmap all confirmed
  rendering correctly from real API responses. Not verified: Crafatar
  avatar images actually loading (this development environment has no
  route to the public internet) — the `<img src>` URLs are correct and
  expected to work wherever this is deployed with real internet access.
- The proxy relocation requires **no code changes** to
  `FoliaRoutesSyncPlugin`/`RouteDiff` — confirmed by inspection and the
  fact that the existing 24-test proxy suite (unchanged) is exactly what
  would catch a regression here.

Not verified — needs your real VPS + home network:

- An actual WireGuard handshake across a real NAT'd home connection and a
  real public VPS IP — the core "no inbound port forward" claim.
- Let's Encrypt certificate issuance against your real domain/DNS.
- A real Minecraft client connecting through the VPS's public `:25565`
  with the proxy relocated.
- The `FoliaNexaStats` plugin actually loading in a live Folia server and
  posting real counters (including live AuraSkills/AxAuctions reads) over
  the tunnel — the plugin itself doesn't exist yet as of this doc (see
  Phase 7).
- Whether the public API's default ~30s cache / 60-per-minute rate limit
  (`Settings.public_api_cache_seconds` / `public_api_rate_limit_per_minute`
  in `mgmt/src/folia_mgmt/config.py`) are actually adequate under real
  traffic — tune once you have real numbers.
