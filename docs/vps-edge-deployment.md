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
sudo ufw allow 19132/udp   # Bedrock/RakNet (Geyser, PLAN.md §7B) — skip if you don't want Bedrock clients
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
subdomain and Caddy never touches it. Bedrock clients connect straight to
the VPS's public IP on `:19132/udp` — Bedrock has no DNS-based SRV
equivalent, so there's no separate record needed for it, PLAN.md §7B.)

## Phase 3: WireGuard tunnel mesh

Run [`deploy/vps/setup-wireguard.sh`](../deploy/vps/setup-wireguard.sh) on
**both** ends — the VPS, and each home box that already runs LXD (no new
hardware needed; one LXD host is enough to start, more can join later, see
below). See that script's own `--help` and
[`deploy/vps/README.md`](../deploy/vps/README.md) for the full two-pass
key-exchange flow; in short, for a first home host named `node-a`:

```bash
# On the VPS:
sudo ./deploy/vps/setup-wireguard.sh --role vps --peer-name node-a
# -> prints the VPS's public key (generated once, reused for every peer)

# On node-a:
sudo ./deploy/vps/setup-wireguard.sh --role home \
  --peer-public-key <vps-pubkey-from-above> \
  --peer-endpoint <vps-public-ip>:51820
# -> writes node-a's full config, prints node-a's own public key

# Back on the VPS, finish the exchange:
sudo ./deploy/vps/setup-wireguard.sh --role vps --peer-name node-a \
  --peer-public-key <node-a-pubkey-from-above> \
  --allowed-ips <node-a-tunnel-ip>/32   # e.g. 10.66.0.2/32

# On both:
sudo systemctl enable --now wg-quick@wg0
```

**Adding a second (or third, ...) home LXD host to the mesh later** is the
same flow with a new `--peer-name` and a distinct `--address` on that
host's own `--role home` run (e.g. `10.66.0.3/24` for a second host,
`10.66.0.4/24` for a third):

```bash
sudo ./deploy/vps/setup-wireguard.sh --role vps --peer-name node-b
# on node-b:
sudo ./deploy/vps/setup-wireguard.sh --role home --address 10.66.0.3/24 \
  --peer-public-key <vps-pubkey> --peer-endpoint <vps-public-ip>:51820
# back on the VPS:
sudo ./deploy/vps/setup-wireguard.sh --role vps --peer-name node-b \
  --peer-public-key <node-b-pubkey> --allowed-ips 10.66.0.3/32
sudo wg syncconf wg0 <(wg-quick strip wg0)   # hot-apply, doesn't drop node-a's session
```

Each home host's `[Peer]` stanza on the VPS lives in its own file under
`peers.d/` next to `wg0.conf` — adding or updating one host's entry never
touches any other's (see the script's own header comment for the full
mechanics). This is what makes per-host [hostname-based routing](#hostname-based-routing-to-different-hosts)
below actually reachable: each `Host` mgmt knows about can have its own
tunnel-scoped `AllowedIPs`, not just "the whole home LAN."

The home side always dials out to the VPS and keeps the NAT mapping alive
(`PersistentKeepalive`) — this is the entire trick for needing no inbound
port forward at home. Once `wg0` is up on both sides:

```bash
sudo wg show   # confirm a recent handshake on both ends
ping 10.66.0.1   # from home, should reach the VPS
ping 10.66.0.2   # from the VPS, should reach home
```

**Scope what the VPS can actually reach.** The script's default
`AllowedIPs` on the VPS side only routes to that one home peer's own
tunnel address — add mgmt's real LAN address and that host's LXD bridge
subnet once you know them (re-run the script with `--peer-name <that-host>
--allowed-ips ...`, or edit that peer's file under `peers.d/` directly and
`wg syncconf`). Do **not** add the
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
this whole setup exists to deliver. If the snap was built with Bedrock
support (PLAN.md §7B — bundled by default), a Bedrock client should be
able to join the same way on `:19132/udp`, no separate relocation step.

## Hostname-based routing to different hosts

If your mesh has more than one home LXD host (Phase 3), you can point
different public domains at different hosts instead of everyone landing on
the same cluster-wide default (PLAN.md §7C). Each domain resolves to
*that host's* own default world — a running `lobby`, else an `overworld`,
same "lobby as hub" preference PLAN.md §14B already uses globally, just
scoped per host:

```bash
folia-nexa-mgmt hosts set-domains node-a smp.example.com
folia-nexa-mgmt hosts set-domains node-b creative.example.com
```

Add a DNS record for each domain pointed at the VPS's public IP (or reuse
`play.<domain>`-style records from Phase 2 — Minecraft's hostname isn't
Caddy-routed, so any A/CNAME record works). `folia-nexa-proxy` picks this
up on its next poll (`FOLIA_ROUTES_POLL_SECONDS`, default 5s) — no proxy
restart needed, matching every other route change in this project.

**This only works for Java clients.** Bedrock/console/mobile clients
connect over RakNet, which has no hostname/SNI concept at all — Geyser has
no way to know which domain a Bedrock player "typed," so Bedrock
connections always land on the cluster's single global default world,
regardless of which domain/IP the client used. This is a protocol
limitation, not a configuration gap — there's nothing to tune here.

## Fanning one proxy out to multiple independent clusters

The section above routes to different **hosts inside one mgmt's own
inventory** — still one mgmt process, one operator. This is different and
broader: **one VPS proxy serving N fully independent FoliaNexa clusters**
(PLAN.md §7D) — separate mgmt processes, separate databases, potentially
separate operators, each with its own WireGuard tunnel peer — so
`{admin,play,api}.domain1.com` lands entirely on domain1's cluster and
`{admin,play,api}.domain2.com` entirely on domain2's, with neither mgmt
aware the other exists.

Three things change from the single-cluster setup earlier in this doc:

1. **A WireGuard peer per cluster.** Repeat Phase 3 once per cluster, each
   with its own `--peer-name`:
   ```bash
   sudo ./deploy/vps/setup-wireguard.sh --role vps --peer-name domain1 \
     --peer-public-key <domain1s-vps-pubkey> --allowed-ips <domain1-tunnel-ip>/32,<domain1-lan-subnet>
   sudo ./deploy/vps/setup-wireguard.sh --role vps --peer-name domain2 \
     --peer-public-key <domain2s-vps-pubkey> --allowed-ips <domain2-tunnel-ip>/32,<domain2-lan-subnet>
   ```
   `setup-wireguard.sh` refuses to add a peer whose `--allowed-ips`
   overlaps an already-configured one — two independent operators' home
   LXD bridges landing on the same default subnet (`192.168.1.0/24` above
   all) is common enough to actually hit, not just a theoretical risk. If
   it refuses, one side needs to renumber its LAN or you need a narrower
   `--allowed-ips`.

2. **A Caddy `clusters.d/` entry per cluster** (Phase 5 below) —
   `add-cluster.sh --id domain1 ...` / `--id domain2 ...`, each pointed at
   that cluster's own `--mgmt-upstream` (its tunnel address from step 1).

3. **`folia-nexa-proxy`'s env config switches from single-cluster to
   multi-cluster shape**, in place of the single `FOLIA_MGMT_URL`/
   `FOLIA_MGMT_API_TOKEN` pair from Phase 4:
   ```bash
   FOLIA_MGMT_CLUSTER_IDS=domain1,domain2
   FOLIA_MGMT_CLUSTER_DOMAIN1_URL=http://<domain1-tunnel-address>:8443
   FOLIA_MGMT_CLUSTER_DOMAIN1_TOKEN=<domain1s-mgmt-api-token>
   FOLIA_MGMT_CLUSTER_DOMAIN2_URL=http://<domain2-tunnel-address>:8443
   FOLIA_MGMT_CLUSTER_DOMAIN2_TOKEN=<domain2s-mgmt-api-token>
   FOLIA_MGMT_PRIMARY_CLUSTER=domain1   # backstops Bedrock + bare-IP Java connections
   ```
   Every registered Velocity server name is qualified with its cluster id
   (`domain1__world-overworld`) so two clusters' same-named worlds never
   collide; each cluster's own `hosts set-domains`-configured domains
   (previous section) are merged into one lookup, first cluster listed
   winning if two operators ever claim the same domain by mistake (logged
   as a warning, not silently dropped). See `FoliaRoutesSyncPlugin`'s
   javadoc for the complete env var reference.

**Bedrock is unchanged** — still always lands on whichever cluster
`FOLIA_MGMT_PRIMARY_CLUSTER` names, for the same RakNet-has-no-hostname
reason as above.

## Phase 5: Caddy — TLS + admin/public-API edge

Copy [`deploy/vps/Caddyfile`](../deploy/vps/Caddyfile) to the VPS, set
`ACME_EMAIL` (env var or edit the file directly), then generate this
cluster's admin/api/portal site blocks with `add-cluster.sh` — one call
per cluster this proxy serves, each writing its own file under
`clusters.d/` that the `Caddyfile` imports:

```bash
cd deploy/vps
./add-cluster.sh --id mycluster \
  --admin-domain admin.example.com --api-domain api.example.com \
  --play-domain play.example.com --mgmt-upstream <mgmt-tunnel-address>:8443
sudo caddy validate --config Caddyfile --adapter caddyfile
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo cp -r clusters.d /etc/caddy/
sudo systemctl reload caddy
```

(A single-cluster setup is just one `add-cluster.sh` call — see "Fanning
one proxy out to multiple independent clusters" below for running several
side by side.)

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

- `deploy/vps/Caddyfile` — validated with a real `caddy validate`, most
  recently Caddy 2.9.1 after it switched from inline `admin`/`api`/`play`
  blocks to `import clusters.d/*.caddy` (PLAN.md §7D, so more than one
  cluster's site blocks can coexist) — confirmed both the empty-
  `clusters.d` default (a Caddy warning, not an error) and a real
  `add-cluster.sh`-generated two-cluster example validate cleanly, and
  that re-running `add-cluster.sh` for one `--id` leaves every other
  cluster's file untouched.
- `deploy/vps/setup-wireguard.sh` — the original single-peer flow (both
  "vps" and "home" roles, the full two-pass key exchange) was run
  end-to-end against real `wireguard-tools`, producing configs that
  `wg-quick strip` parses without error. The newer multi-peer `peers.d`
  mechanics on `--role vps` (adding/updating one home host's `[Peer]`
  stanza without disturbing another's, regenerating `wg0.conf` from
  `[Interface]` + every file in `peers.d/`) were exercised with a fake
  `wg` binary standing in for key generation, not real `wireguard-tools` —
  no `wg` binary was available in the environment this was added in.
  Confirmed by that dry run: two peers added in sequence both survive in
  the final `wg0.conf`, re-running with an existing `--peer-name` replaces
  only that peer's block, and `--role home` is byte-for-byte unchanged.
  Not run against two-plus real machines actually establishing live UDP
  handshakes across a real NAT, and `wg syncconf`'s claimed no-drop
  hot-reload of one new peer alongside an already-connected one isn't
  verified against a real `wg0` interface — that needs your real VPS and
  home network(s). The newer `AllowedIPs` overlap check (PLAN.md §7D) was
  exercised with that same fake-`wg` dry run: confirmed it accepts two
  peers with disjoint LAN subnets, refuses a third whose subnet collides
  with an existing one (naming both peers in the error), and doesn't
  false-positive when re-running an existing peer's own name unchanged.
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
  fact that the existing proxy test suite (35 tests as of hostname-based
  routing, PLAN.md §7C) is exactly what would catch a regression here.
- Hostname-based routing (`Host.domains`, `GET /api/v1/routes`'s `domains`
  map, `FoliaRoutesSyncPlugin`'s `VirtualHostRouter`) — real pytest suite
  on the mgmt side (`test_hosts.py`, `test_routes.py`) and a real Gradle
  test run on the proxy side (`RoutesJsonTest`, `VirtualHostRouterTest`,
  `MgmtRoutesClientTest`). Not verified: a real Java Minecraft client
  actually connecting with a specific hostname and landing on the right
  host's world — no live Velocity server or Minecraft client was available
  to test against in this environment.
- Multi-cluster fan-out (PLAN.md §7D) — `ClusterConfig`, `ServerName`,
  `DomainRouteMerger`, and the `FoliaRoutesSyncPlugin` refactor built on
  them (per-cluster server-name qualification, merged domain routing,
  per-cluster access-gate/chat/MOTD resolution) — real Gradle test run,
  62 tests total. Not verified: a real Java client actually connecting to
  two genuinely independent mgmt clusters through one live proxy and
  landing on the correct one, or a real Discord chat bridge round-trip
  scoped to just one cluster's players — no live infrastructure for
  either in this environment.

Not verified — needs your real VPS + home network:

- An actual WireGuard handshake across a real NAT'd home connection and a
  real public VPS IP — the core "no inbound port forward" claim.
- Let's Encrypt certificate issuance against your real domain/DNS.
- A real Minecraft client connecting through the VPS's public `:25565`
  with the proxy relocated.
- A real Bedrock client connecting through `:19132/udp` (PLAN.md §7B) —
  no Bedrock/console/mobile client or live GeyserMC build was reachable
  to test against in this environment. The bundled Geyser-Velocity/
  floodgate-velocity jars were downloaded for real and sha256-verified
  against GeyserMC's own build API (see the `geyser-plugins` part in
  `proxy/snapcraft.yaml`), but `snapcraft` itself wasn't available to
  re-run the full snap build in the environment this addition was made
  in — see that file's own comments for the exact verification done.
- The `FoliaNexaStats` plugin actually loading in a live Folia server and
  posting real counters (including live AuraSkills/AxAuctions reads) over
  the tunnel — the plugin itself doesn't exist yet as of this doc (see
  Phase 7).
- Whether the public API's default ~30s cache / 60-per-minute rate limit
  (`Settings.public_api_cache_seconds` / `public_api_rate_limit_per_minute`
  in `mgmt/src/folia_mgmt/config.py`) are actually adequate under real
  traffic — tune once you have real numbers.
- A real multi-peer WireGuard mesh spanning two (or more) *genuinely
  separate operators'* networks, as opposed to one operator's several
  home hosts (which is what the multi-peer work above actually
  exercised) — including whether their real-world LAN/tunnel addressing
  ends up colliding in practice the way the `AllowedIPs` overlap check
  is designed to catch.
