# Folia Multi-World SMP Cluster: Architecture & Implementation Plan (v2)

**Control Plane:** `folia-nexa-mgmt` (snap) — orchestrator, scheduler, REST API, web dashboard
**Compute Agent:** `folia-nexa-node` (snap) — runs inside every world's LXD container, runs the JVM, reports health
**Edge:** `folia-nexa-proxy` (snap) — public entry point, routing table synced live from mgmt
**Substrate:** One or more standalone LXD hosts, each trusted individually by `folia-nexa-mgmt` over the LXD remote API
**Application Packaging:** Snaps with `systemd` daemon supervision throughout

> Supersedes the static-topology plan in `PLAN.md.old`. That version hardcoded four fixed containers (`folia-smp`, `folia-nether`, `hub-lobby`, `edge-proxy`) pinned to specific cores on one box. This version replaces the fixed topology with a scheduler: any number of LXD hosts contribute capacity, and `folia-nexa-mgmt` decides which worlds (overworld, nether, end, lobby, minigames, ephemeral staging, …) run where, and moves them around as capacity changes.

---

## 1. Architecture Overview

Three roles, cleanly separated:

- **`folia-nexa-mgmt`** never runs a Minecraft process itself. It holds cluster state (which hosts exist, which worlds should exist, where each is currently placed), talks to each LXD host's remote API to create/destroy/snapshot containers, and exposes a REST API + web dashboard for operators.
- **`folia-nexa-node`** is baked into (or installed at first boot of) every world's container. It never talks to the scheduler to ask "what should I run" — its instance already knows, because `folia-nexa-mgmt` wrote that assignment into the container's own LXD instance config at creation time. The node agent's job is: fetch the jar + plugins, run the JVM, expose local health/TPS metrics, restart on crash.
- **`folia-nexa-proxy`** is the single public-facing port. It doesn't hardcode a server list — it polls `folia-nexa-mgmt`'s routing API and rebuilds its backend list as worlds come and go.

```
                                   [ Public Internet ]
                                            │
                                    (Port 25565/TCP)
                                            ▼
                          ┌──────────────────────────────────┐
                          │  folia-nexa-proxy (dynamic routes)  │
                          └──────────────────┬─────────────────┘
                                            │  polls /api/v1/routes
                                            ▼
┌───────────────────────────────────────────────────────────────────────┐
│                          folia-nexa-mgmt                                 │
│  • Host registry (trusted LXD remotes)                                  │
│  • World registry (desired state) + scheduler (reconcile loop)          │
│  • REST API + Web dashboard                                             │
└───────┬────────────────────────────┬────────────────────────────┬──────┘
        │ LXD remote API (mTLS)      │ LXD remote API (mTLS)      │ LXD remote API (mTLS)
        ▼                            ▼                            ▼
┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
│ LXD host: node-a      │   │ LXD host: node-b      │   │ LXD host: node-c      │
│ project: folia        │   │ project: folia        │   │ project: folia        │
│ ┌───────────────────┐ │   │ ┌───────────────────┐ │   │ ┌───────────────────┐ │
│ │ world-overworld    │ │   │ │ world-lobby        │ │   │ │ world-minigame-sg  │ │
│ │ folia-nexa-node     │ │   │ │ folia-nexa-node     │ │   │ │ folia-nexa-node     │ │
│ └───────────────────┘ │   │ └───────────────────┘ │   │ └───────────────────┘ │
│ ┌───────────────────┐ │   │                        │   │                        │
│ │ world-nether       │ │   │                        │   │                        │
│ │ folia-nexa-node     │ │   │                        │   │                        │
│ └───────────────────┘ │   │                        │   │                        │
└──────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

`node-a`, `node-b`, `node-c` can be one box (today: the Core Ultra 5 235T) or many. Adding capacity is "trust another LXD host," not "edit a topology diagram."

---

## 2. Core Concepts & Data Model

### Host

A **Host** is a standalone LXD daemon that `folia-nexa-mgmt` has been granted restricted, project-scoped access to. Mgmt tracks:

```yaml
host:
  name: node-a
  address: 10.0.1.11:8443
  project: folia               # LXD project this cert is restricted to
  cert_fingerprint: 3a:9f:...
  labels: {cpu_type: p-core, zone: rack1}
  capacity: {cpu_cores: 6, memory_gb: 16}
  status: online                # online | offline | draining | cordoned
```

Capacity numbers come from the LXD project's quota (`limits.cpu`, `limits.memory`), not raw host specs — mgmt only ever schedules against what it's actually been granted, which is what makes shared (non-dedicated) hosts safe to add.

`online`/`offline` are set automatically: every reconcile pass pings each
trusted host's LXD API directly (`scheduler.check_host_health`) and flips
status based on reachability, so a host that loses power or network stops
showing as `online` within one reconcile interval. `draining`/`cordoned`
are exclusively operator-set (`POST /hosts/{name}/drain`, or a future
cordon action) and are never touched by the automatic health check —
but draining a host *does* actively evacuate it: see §5's "Host health"
section for the migration behavior that runs against it.

### World

A **World** is the schedulable unit: one Folia/Paper server, one LXD container, one JVM.

```yaml
world:
  name: world-nether
  type: nether                  # overworld | nether | end | lobby | minigame | proxy | staging | infra
  jar: { engine: folia, version: "1.21.4" }
  plugins: [HuskClaims, AuraSkills, Spark, ...]
  resources: { cpu_cores: 2, memory_gb: 3 }
  placement:
    labels: { cpu_type: e-core }   # optional affinity, e.g. pin heavy worlds to p-core hosts
    sticky_host: null              # once scheduled, world stays put unless drained (stateful data lives with it)
  snapshot_policy: { schedule: "@hourly", expiry: "24h" }
  status: { phase: running, host: node-a, container: world-nether, tps: 19.98, players: 12 }
```

Worlds are **stateful and sticky**: once placed, a world stays on its host (its data lives in that container's storage) unless an operator explicitly drains the host or migrates the world (snapshot → copy to new host → delete original — see §13). This avoids building live cross-host storage migration for v1.

`type: infra` exists in the schema for a Folia/Paper-based shared dependency a scheduler-placed world could point at, excluded from `folia-nexa-proxy`'s route table by default. In practice, neither shared dependency this project actually needed turned out to fit that mold: the MySQL/MariaDB instance backing LuckPerms (§11B) and `folia-nexa-bot` (§16) both run things folia-nexa-node can't (a database server; a standalone Discord bot process) and are provisioned/installed directly rather than scheduled as worlds. `type: infra` is left in place for a future case that's actually a Folia/Paper process, but has no real user yet.

### Reconcile loop

`folia-nexa-mgmt` is a controller: it compares desired world list against actual LXD state on every trusted host, and acts on drift — create a container for a pending world, restart a crashed one, tear down a deleted one. Same shape as any k8s-style controller, scaled down to this problem.

---

## 3. LXD Host Trust & Safety Model

Adding a host is a one-time admin action, not a self-registration handshake — mgmt already knows everything about a world before it exists, because mgmt created it.

**On the LXD host (once):**
```bash
# Expose the remote API (off by default — unix socket only until this is set)
lxc config set core.https_address ":8443"

# Create an isolated, quota-capped project for folia workloads
lxc project create folia \
  -c limits.cpu=6 -c limits.memory=16GB -c limits.containers=20 \
  -c restricted=true -c restricted.containers.nesting=block

# Generate a one-time trust token for folia-nexa-mgmt to consume
lxc config trust add --name folia-nexa-mgmt --restricted --projects folia
# -> prints a one-time token
```

**From `folia-nexa-mgmt`'s dashboard or CLI:**
```bash
folia-nexa-mgmt hosts add node-a --address 10.0.1.11:8443 --token <one-time-token>
```

This performs the standard LXD trust exchange (mTLS, client cert generated and stored under `$SNAP_COMMON/mgmt/certs/`) and records the host. From this point mgmt's certificate is **restricted to the `folia` project** — it cannot see or touch any other project, storage pool, or host-level setting on that machine, even if the mgmt host itself is compromised. Blast radius of a leaked mgmt credential is "the folia project's containers on that one host," never the host itself or other tenants.

**Network requirement:** the LXD API port (8443) must be reachable from `folia-nexa-mgmt`, and must **not** be exposed to the public internet — put it on a private management VLAN or a WireGuard mesh between mgmt and every host. This is the one hard networking requirement multi-host introduces; single-host deployments can just bind it to loopback/private bridge.

A host is **not required to be dedicated** to Folia — the project quota is the isolation boundary. A shared LXD box can host a `folia` project alongside unrelated projects as long as its quota reflects real spare capacity.

---

## 4. Automated Host Enrollment (`folia-host-join`)

Manually running the trust exchange from §3 on both sides doesn't scale past "the one box I'm sitting at." `tools/folia-host-join.sh` (in this repo) automates the entire host side of it down to one command, given the mgmt URL and a short-lived join token.

Two distinct tokens are involved, and it's worth being precise about what each one is for:

| Token | Issued by | Lifetime | Proves |
| --- | --- | --- | --- |
| **Join token** | `folia-nexa-mgmt` (admin requests it via dashboard/CLI) | Short-lived (default 15m), single-use | "Whoever holds this is authorized to enroll one new host into *this* cluster" — the control that stops a random machine from adding itself as compute capacity. |
| **LXD trust token** | The host's own LXD daemon, generated by the script | Single-use, consumed immediately | Standard LXD mechanism that lets one specific client certificate (mgmt's) become permanently trusted by that host, scoped to the `folia` project. |

Flow:

1. Admin: `folia-nexa-mgmt hosts create-join-token` (or the dashboard's "Add Host" button) → prints a join token.
2. Admin, on the target host:
   ```bash
   sudo ./tools/folia-host-join.sh \
     --mgmt-url https://mgmt.internal:8443 \
     --join-token <token-from-step-1> \
     --address 10.0.1.12
   ```
3. The script:
   - Confirms LXD is installed and initialized (won't run `lxd init` for you — storage pool choice is a decision, not a default).
   - Enables the remote API (`core.https_address`) if not already set.
   - Creates the `folia` project with quotas if it doesn't exist, sized from detected `nproc`/memory (overridable with `--cpu`/`--memory`, always printed for confirmation before applying).
   - Generates a fresh LXD trust token scoped to that project.
   - Calls `POST /api/v1/hosts/enroll` on mgmt, authenticated with the join token, carrying the LXD trust token + detected capacity.
   - Mgmt redeems the LXD trust token itself (completing the mTLS handshake — the same "add a trusted client" step from §3, just performed by mgmt instead of a human running `lxc remote add`) and records the host.
4. `GET /api/v1/hosts` on mgmt shows the new host `online` within a few seconds.

`--skip-enroll` stops after generating the LXD trust token and prints it instead of calling mgmt — useful for finishing the trust exchange manually, or for exercising the host-side steps before `folia-nexa-mgmt` itself exists.

> **Status:** the script is real and safe to run today for the LXD-prep steps (project/quota/https-address/trust-token). The final `POST /api/v1/hosts/enroll` call will fail until `folia-nexa-mgmt`'s API (§10) is actually implemented — the script targets that contract now so nothing has to change on the host side once mgmt catches up.

---

## 5. Scheduler

### Host health

Before anything else, each reconcile pass pings every trusted host not
currently `draining`/`cordoned` (LXD's own `GET /1.0`, mTLS-pinned the same
as every other call) and writes `online`/`offline` back to the DB based on
whether it answered — a single failed ping is enough to mark a host
offline, matching how a single failed `/healthz` poll already marks a
world crashed below; there's no consecutive-failure debounce. This runs
first in the pass specifically so a host that just went dark is excluded
from that same tick's placement decisions, not just the next one.

### Draining a host

`POST /hosts/{name}/drain` sets `Host.status: draining` — this both stops
new placements from landing there (the `status: online` filter in
Placement below already excludes it) and, since `scheduler.
migrate_worlds_off_draining_hosts` runs every reconcile pass, actively
evacuates it: every world still sitting on a draining host is migrated
(stop → export → import → start, same as the manual per-world
`POST /worlds/{name}/migrate`, §13 — a brief outage is expected) to
whichever `online` host currently has the most free capacity left after
the move. A world that can't be moved yet (no online host with room)
stays put and is retried on the next pass. There is no "undrain" — taking
a drained host back into service is a direct DB edit or re-enrollment,
not an API call, since nothing in this codebase ever needed one before.

### Placement

On each reconcile pass, for every world in `phase: pending`:

1. Filter hosts to those matching the world's `placement.labels` (if any) and `status: online`.
2. Filter to hosts with enough *unallocated* capacity (`capacity - sum(resources of worlds already placed there)`) for the world's `resources` request.
3. Pick the host with the most free capacity after placement (bin-pack the fullest-fit-remaining, not first-fit — keeps headroom spread evenly rather than stacking everything on one box).
4. Call the host's LXD API: launch a container from the base image, apply CPU/memory limits, write world assignment into instance config (see §9), apply the world's `snapshot_policy`.
5. Mark the world `phase: provisioning` until `folia-nexa-node` reports itself healthy, then `phase: running`.

### World lifecycle states

`pending → provisioning → running → (crashed → restarting) → draining → deleted`

- **crashed**: node agent's local health endpoint stops responding or JVM exits non-zero; mgmt restarts the container (not a fresh reschedule — data and identity stay put).
- **draining** (a *World*'s phase, distinct from a *Host*'s `draining` status above): operator-initiated via `DELETE /worlds/{name}` — a transient state on the way to permanent deletion, not a stop/park state; `scheduler.teardown_world` deletes the container and hard-deletes the row as soon as it's confirmed gone. Snapshot first (`worlds snapshot`) if the data needs to survive — this is not reversible. Evacuating a world off a *host* being decommissioned, without deleting it, is the `Host.status: draining` behavior in "Draining a host" above instead.

### What the scheduler deliberately does *not* do (v1)

- No live migration of a running world between hosts (worlds are sticky; migration is snapshot-based and requires a brief restart — see §13).
- No autoscaling of minigame instance counts. Operators/dashboard create additional `minigame` worlds explicitly; the scheduler just places them.

---

## 6. Snapshot & Backup Policy

Every world's `snapshot_policy` is written straight into LXD's own scheduled-snapshot config on the instance — no custom cron/ZFS scripts:

```bash
lxc config set world-overworld snapshots.schedule "@hourly"
lxc config set world-overworld snapshots.expiry "24h"
```

Defaults by world type (overridable per-world):

| World type | Schedule | Expiry |
| --- | --- | --- |
| `overworld` / `nether` / `end` (persistent, player-built) | `@hourly` | `7d` |
| `lobby` (config-driven, rarely mutated) | `@daily` | `3d` |
| `minigame` (arena resets each match) | none | — |
| `staging` (ephemeral test clone) | none | — |

This is strictly better than the old plan's `provision_staging.sh`/manual `lxc snapshot` calls: rollback for *any* world is `lxc restore world-nether <snapshot-name>`, no bespoke tooling needed.

### 6A. Dashboard-managed automatic backups ("time machine" restores)

The `snapshot_schedule`/`snapshot_expiry` LXD-native mechanism above is only ever applied at container **launch** time — nothing re-pushes it on edit, and mgmt never reads LXD's own snapshot list back, so there was no way to browse or restore from it in the dashboard. A separate, mgmt-driven mechanism sits alongside it for that:

- `World.backups_enabled` (default `true`) gates a per-world automatic backup, toggled from the dashboard's world details panel (`PUT /worlds/{name}/backups-config`, operator+) or the API — independent of `snapshot_schedule`.
- `scheduler.run_scheduled_backups`, one of `reconcile()`'s steps (§5), takes an LXD instance snapshot (`LXDClient.snapshot_container`) of every `backups_enabled` running world once an hour, tracked in a new `WorldBackup` row (world name, snapshot name, `kind="scheduled"`, timestamp) — self-gated to hourly by comparing against each world's own most recent row, since `reconcile()` itself ticks every 15s. Every attempt (success or failure) also stamps `World.last_backup_attempt_at`/`last_backup_error` — a scheduled backup that starts failing every hour (unreachable host, full storage pool, whatever) used to do so silently forever, with only a swallowed-by-`_isolated` log line as any trace; the dashboard's Backups section now surfaces that error string directly instead of just showing an empty list with no explanation, and it's cleared back to `None` the next time a backup for that world actually succeeds.
- `scheduler.prune_expired_backups`, the same loop, deletes both the row and the underlying LXD snapshot once older than a week (`BACKUP_RETENTION`), keeping "a week's worth" per world. A snapshot-delete failure (host briefly unreachable) leaves the row in place for the next tick to retry, rather than losing track of a snapshot still sitting on disk. Applies equally to `kind="manual"` rows below — there's no separate retention tier for an on-demand backup.
- `GET /worlds/{name}/backups` (viewer+) lists a world's tracked backups newest-first — what the dashboard's "Backups" section (in the world details panel, alongside RCON/ops/logs) renders as a restore list.
- `POST /worlds/{name}/backups/manual` (operator+) takes a backup right now instead of waiting for the next hourly window — e.g. right before a risky plugin upgrade — writing a `kind="manual"` `WorldBackup` row that shows up in, and can be restored from, the exact same list as scheduled backups. Works regardless of `backups_enabled`, which only gates the *automatic* hourly schedule, not an operator's own explicit request. The dashboard's Backups section has a "Back up now" button wired to this.
- `POST /worlds/{name}/backups/{backup_id}/restore` is **admin-only** (stricter than the plain operator-gated `POST /worlds/{name}/restore/{snapshot_name}` above, since this is reachable straight from the dashboard by any logged-in operator otherwise) — rolls the world back to that snapshot via the same `LXDClient.restore_snapshot` LXD already exposes.

Real pytest coverage in both `mgmt/tests/test_scheduler.py` (the hourly-gating and retention-pruning logic in isolation, including snapshot/delete failure retry paths, and that a failure/success correctly stamps/clears `last_backup_error`) and `mgmt/tests/test_worlds.py` (the API endpoints end-to-end through a real reconcile pass, the manual-backup endpoint including its operator-gate and not-yet-placed 409, and the admin-vs-operator restore gate). The dashboard's new Backups section was also loaded in real headless Chromium against a real running `folia-nexa-mgmt` instance, confirming the enable/disable toggle round-trips through the real API and the empty-state/backup-list rendering — not click-through tested for the restore or "Back up now" buttons (no real LXD daemon or placed world available in that check, same caveat as the rest of this dashboard's manually-traced flows per CLAUDE.md). The `last_backup_error` surfacing is the fix for a real operator report (2026-08-23): automatic backups enabled but nothing ever showing up in the list — since `LXDClient.snapshot_container` itself has never been exercised against a real LXD daemon (see CLAUDE.md), a failing snapshot call there would have produced exactly that symptom with zero visible explanation before this fix; the underlying `snapshot_container` call itself is still unverified against live LXD, same as before.

---

## 7. Networking & Edge Proxy

Each host's `folia` project containers sit on that host's local LXD bridge. `folia-nexa-proxy` needs a routable path to every world's Minecraft port (25565 inside each container) regardless of which host it lands on — cross-host reachability is the one piece of real infrastructure this design requires beyond a single box:

- **Single host (today):** trivial — everything's on `lxdbr0`, no extra work.
- **Multi-host:** put all hosts + `folia-nexa-proxy` on a WireGuard mesh (or LXD OVN with a shared uplink network) so container IPs are mutually routable, or `lxc config device add` a `proxy` NIC device that publishes each world's port on the host's own address and give mgmt a stable `host-ip:published-port` per world instead of a container IP. Start with the WireGuard mesh — it composes cleanly with the trust model in §3 (same private network the LXD API traffic already lives on).

`folia-nexa-proxy` polls `GET /api/v1/routes` on mgmt every few seconds:

```json
{
  "routes": [
    {"world": "world-overworld", "type": "overworld", "address": "10.0.1.21:25565"},
    {"world": "world-nether",    "type": "nether",    "address": "10.0.1.22:25565"},
    {"world": "world-lobby",     "type": "lobby",     "address": "10.0.2.10:25565", "default": true}
  ]
}
```

and reconciles its own `server-list`/forwarding config (via Velocity's plugin API) against it — no restart required to add or remove a world. Exactly one route is flagged `default`: a running `lobby`-type world if one exists, else the fallback is a running `overworld` (`routers/routes.py`'s `_pick_default` — see §14B for the lobby-as-hub design this exists for).

---

## 7A. VPS Edge & Public Portal (Implemented, v1)

A concrete instance of §7's multi-host reachability problem, extended one
hop further: instead of (or in addition to) a home LAN's hosts, one peer
on the WireGuard mesh is a public VPS (Linode or otherwise) with no
inbound port forwarding required at home. This supersedes §7's abstract
"put everything on a WireGuard mesh" sketch with a concrete, implemented
shape — see `docs/vps-edge-deployment.md` for the operator walkthrough and
`deploy/vps/` for the actual config (WireGuard setup script, Caddyfile).

**What moves to the VPS:** `folia-nexa-proxy` itself (it was always meant
to be "the single public-facing port" — this just gives it a real public
box, and now a second protocol too, see §7B), Caddy (TLS termination +
reverse proxy), and the static player portal (`portal/`). **What stays
home:** `folia-nexa-mgmt`, the LXD hosts, every world. mgmt is reachable from the VPS only over the WireGuard
tunnel, with `AllowedIPs`/firewall rules scoped to just what the relocated
proxy and Caddy actually need — the LXD hosts' own remote API stays off
that path entirely, unchanged from §3's rule that it must never be
internet-adjacent.

Three public subdomains, terminated by Caddy on the VPS:
`admin.<domain>` (mgmt's dashboard/API, reverse-proxied, still behind its
existing bearer-token auth — unchanged), `api.<domain>` (a new
unauthenticated public API, see below), `play.<domain>` (the static
portal).

**Public player-hub API (`GET /api/v1/public/*`,
`mgmt/src/folia_mgmt/routers/public_stats.py`):** leaderboards, player
profiles, playtime heatmaps, and player-face avatars — deliberately
unauthenticated, same rationale as `/plugins-manifest` in §14: everything
it returns is already meant to be public. This is the first mgmt surface
designed to take real internet traffic, so it carries its own in-process
TTL cache and per-IP rate limit (`Settings.public_api_cache_seconds` /
`public_api_rate_limit_per_minute`) as defense in depth under whatever
Caddy adds in front. Fed by a new ingestion endpoint (`POST
/api/v1/stats/report`, operator-role token, `routers/stats.py`) that a new
in-house plugin — catalog id `FoliaNexaStats`, released and deployed —
reports to periodically via `AsyncScheduler`, softdepending on
`AuraSkills`/`AxAuctions` for two extra stat keys when either is present
on a world. Avatars (`GET /public/players/{uuid}/avatar`) are rendered by
mgmt itself (`avatar.py`, Pillow) from the player's real skin fetched off
Mojang's session server — self-hosted rather than proxying a third-party
CDN, after crafatar.com (the originally-planned provider, see §16) had a
real outage that broke every avatar on the portal at once.

This is a deliberately scoped-down v1 of §16's original "Public Community
& Analytics Portal" vision below — see that section for what's still
aspirational beyond this. New tables (`PlayerProfile`, `PlayerStat`,
`PlayerPlaytimeDaily` in `mgmt/src/folia_mgmt/models.py`) reuse mgmt's
existing SQLModel/SQLite stack rather than introducing Redis/Postgres/
ClickHouse; `portal/` is hand-written static HTML/JS with no build step or
Node.js tooling, matching mgmt's own dashboard rather than adopting
Next.js/Astro; there's no WebSocket live-push layer — "recently active"
on the portal's home page is a client-side approximation from stats
report recency, clearly labeled as such rather than claimed as exact
real-time presence.

---

## 7B. Bedrock Client Support (GeyserMC + Floodgate)

Bedrock (console/mobile/Windows 10) players speak a different protocol
family entirely (RakNet over UDP, not Java Edition's TCP protocol), so
the cluster's single public-facing proxy is exactly where to terminate
it — same rationale as §7A's "the proxy was always meant to be the single
public-facing port," extended to a second protocol on the same box
instead of a second box.

**What changed:** `folia-nexa-proxy`'s snap now bundles two more Velocity
plugins alongside `folia-routes-sync` — Geyser-Velocity (Bedrock↔Java
protocol translation) and floodgate-velocity (lets Bedrock/Xbox accounts
that don't own Java Minecraft join at all, assigning them a deterministic
UUID derived from their Xbox XUID). Both are ordinary Velocity plugin
jars, fetched at snap-build time by a new `geyser-plugins` part in
`proxy/snapcraft.yaml` — no changes to `FoliaRoutesSyncPlugin` or any
backend world were needed, since Velocity already forwards whatever
connection Geyser hands it through the existing backend server list
(§7's routing) exactly like any Java client. Bedrock clients connect to
the proxy host's `:19132/udp` (Geyser's default; changeable by editing
its auto-generated `config.yml` under `$SNAP_COMMON/proxy/plugins/
Geyser-Velocity/` after first start — same "seed once, never overwrite"
pattern `run-velocity.sh` already applies to `velocity.toml`).

Unlike `velocity-runtime`'s pinned Velocity version, the `geyser-plugins`
part deliberately tracks GeyserMC's `latest` build rather than pinning
one: Velocity is pinned for backend-protocol stability, but an old,
pinned Geyser build breaks newly-released Bedrock client versions
outright, and Mojang ships those on its own schedule this project has no
control over.

**Per-world Bedrock-awareness (optional):** the proxy-level integration
alone is sufficient for Bedrock players to join and play. If a specific
world should also *recognize* a player joined via Bedrock (correct
skin/identity on that world specifically, or a plugin using the Floodgate
API), install the `Floodgate` catalog entry
(`mgmt/src/folia_mgmt/catalog.yaml`) on it via `--plugin Floodgate`. This
needs one manual, one-time step this project doesn't automate: after the
proxy has started once (so its own bundled Floodgate has generated a
keypair), copy `$SNAP_COMMON/proxy/plugins/floodgate/key.pem` from the
proxy host to that world's `plugins/floodgate/key.pem` and restart the
world. There's no proxy→world file-push channel today the way
`luckperms.py` pushes LuckPerms' `config.yml` to worlds mgmt-side — that
would be the natural next step if this needs automating later, but it's
out of scope for now (a single manual copy, done once per world that
opts in, not a recurring operational burden).

**Access gate + Bedrock:** Floodgate UUIDs are syntactically ordinary
RFC-4122 UUIDs, so `FOLIA_ACCESS_GATE_ENABLED`'s approved-UUID gate
(§11C) needed no changes on the proxy side — `ApprovedPlayers.java`
already parses any well-formed UUID, and `AccessRequest.minecraft_uuid`
(`mgmt/src/folia_mgmt/models.py`) is an unvalidated plain string column.
The one real gap was upstream of that: the Discord bot's
`/request-access` command only ever resolved a Java username through the
Mojang API (`mgmt/src/folia_mgmt/discord.py::resolve_minecraft_uuid`),
with no way to register a Bedrock player's Floodgate UUID at all. Fixed
by adding an optional `minecraft_uuid` parameter to `/request-access`
(`bot/src/folia_bot/bot.py`) and `POST /api/v1/access-requests`
(`routers/access_requests.py`) — when supplied, mgmt stores it directly
and skips the Mojang lookup. A Bedrock player finds their own Floodgate
UUID via Floodgate's in-game `/uuid` command after any Geyser-fronted
join. The web OAuth flow (`GET /auth/discord/callback`) still has no
equivalent — it carries the username through Discord's `state` redirect
param with no room for a second field — and would need frontend changes
to support directly; left as a documented follow-up, not implemented.

**What's real vs. unverified:** the two GeyserMC download URLs the new
snapcraft part uses, and the `Floodgate` catalog entry's own download
URL, were fetched for real and their sha256 checked against GeyserMC's
build-metadata API in the environment this was added in. Not verified:
the full `snapcraft` build of the updated `proxy/` snap (no
`snapcraft`/`snapd` available in that environment), and — like the rest
of §7A — an actual Bedrock/console/mobile client joining for real (no
such client was reachable to test against). See `CLAUDE.md`'s own
"what's real" section for the same breakdown.

---

## 8. Snap Packaging Specifications

### A. `folia-nexa-mgmt` snap

```yaml
name: folia-nexa-mgmt
version: '0.1'
summary: Folia cluster orchestrator — scheduler, REST API, and dashboard
description: Places Folia/Paper worlds across trusted LXD hosts and tracks cluster state.
base: core24
confinement: strict

apps:
  daemon:
    command: bin/run-mgmt.sh
    daemon: simple
    restart-condition: on-failure
    plugs: [network, network-bind]

parts:
  mgmt:
    plugin: python
    source: .
    python-requirements: [requirements.txt]   # fastapi, uvicorn, pylxd, pydantic
    override-build: |
      craftctl default
      mkdir -p $CRAFT_PART_INSTALL/bin
      cat << 'EOF' > $CRAFT_PART_INSTALL/bin/run-mgmt.sh
      #!/bin/bash
      export MGMT_STATE_DIR="$SNAP_COMMON/mgmt"
      mkdir -p "$MGMT_STATE_DIR/certs"
      exec $SNAP/bin/uvicorn folia_mgmt.main:app \
        --host 0.0.0.0 --port 8443 \
        --ssl-keyfile "$MGMT_STATE_DIR/certs/dashboard.key" \
        --ssl-certfile "$MGMT_STATE_DIR/certs/dashboard.crt"
      EOF
      chmod +x $CRAFT_PART_INSTALL/bin/run-mgmt.sh
```

No `lxd` socket plug — mgmt only ever talks to LXD over the *remote* HTTPS API (`network` plug), including for a host that happens to be colocated on the same machine. One code path for local and remote hosts.

### B. `folia-nexa-node` snap

```yaml
name: folia-nexa-node
version: '0.1'
summary: In-container Folia/Paper world runner and health agent
description: Runs the JVM for a single world; reads its assignment from the container's own LXD instance config.
base: core24
confinement: strict

environment:
  JAVA_HOME: $SNAP/usr/lib/jvm/java-21-openjdk-amd64
  PATH: $SNAP/usr/lib/jvm/java-21-openjdk-amd64/bin:$PATH
  WORLD_DIR: $SNAP_COMMON/world

apps:
  daemon:
    command: bin/run-node.sh
    daemon: simple
    restart-condition: on-failure
    restart-delay: 5s
    plugs: [network, network-bind]

parts:
  node-runtime:
    plugin: dump
    source: .
    stage-packages: [openjdk-21-jre-headless]
    override-build: |
      craftctl default
      mkdir -p $CRAFT_PART_INSTALL/bin
      cat << 'EOF' > $CRAFT_PART_INSTALL/bin/run-node.sh
      #!/bin/bash
      set -euo pipefail
      # Read this container's own assignment via the devlxd socket — no
      # network call to mgmt needed just to find out what to run.
      exec $SNAP/bin/folia-node-agent \
        --devlxd-socket /dev/lxd/sock \
        --world-dir "$WORLD_DIR" \
        --java "$JAVA_HOME/bin/java"
      EOF
      chmod +x $CRAFT_PART_INSTALL/bin/run-node.sh
```

### C. `folia-nexa-proxy` snap

Unchanged from the old plan's runtime (Velocity + Java), plus a routes-sync plugin dropped into `$SNAP_COMMON/proxy/plugins/` that polls mgmt's `/api/v1/routes` and calls Velocity's `ProxyServer.registerServer()` / `unregisterServer()` on diff. `plugs: [network, network-bind]`, unchanged confinement.

---

## 9. `folia-nexa-node` Runtime Behavior

When mgmt creates a world's container, it writes the assignment straight into LXD instance config at launch time:

```bash
lxc launch images:folia-node-base world-nether \
  --project folia \
  -c limits.cpu=2 -c limits.memory=3GB \
  -c user.folia.world-name=world-nether \
  -c user.folia.world-type=nether \
  -c user.folia.jar-url=https://artifacts.internal/folia/1.21.4/folia.jar \
  -c user.folia.plugins-manifest-url=https://artifacts.internal/folia/manifests/world-nether.json \
  -c snapshots.schedule="@hourly" -c snapshots.expiry="24h"
```

Inside the container, `folia-nexa-node` reads those `user.folia.*` keys over the **devlxd socket** (`/dev/lxd/sock`, always present, no network config or credentials needed) — this is the same mechanism cloud-init uses inside LXD containers. On start it:

1. Reads its assignment (world name/type, jar URL, plugin manifest URL).
2. Downloads the jar + plugins into `$SNAP_COMMON/world` if not already staged (idempotent — restarts don't re-download).
3. Launches the JVM with region-scheduler-friendly flags (same `-XX:+UseZGC -XX:+ZGenerational` baseline as the old plan).
4. Serves a local `GET /healthz` and `GET /metrics` (TPS, tick times, memory, player count) on a loopback-bound port that mgmt scrapes over the container's network address.
5. On JVM exit, reports the crash reason locally (log tail) so mgmt's restart doesn't lose the "why."

No join token, no outbound registration call — the container's own config *is* its registration, and mgmt already knows it exists because mgmt is the one that ran `lxc launch`.

---

## 10. REST API Reference (`folia-nexa-mgmt`)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/hosts/join-token` | Issue a short-lived, single-use token for `folia-host-join` (admin only) |
| `POST` | `/api/v1/hosts/enroll` | Consumed by `folia-host-join`; redeems the host's LXD trust token and registers it |
| `GET` | `/api/v1/hosts` | List hosts, capacity, utilization |
| `POST` | `/api/v1/hosts/{name}/drain` | Cordon + evacuate a host |
| `POST` | `/api/v1/worlds` | Declare a new world (desired state) |
| `GET` | `/api/v1/worlds` | List worlds + live status/metrics |
| `DELETE` | `/api/v1/worlds/{name}` | Tear down a world (snapshots retained per policy until expiry) |
| `POST` | `/api/v1/worlds/{name}/snapshot` | On-demand snapshot |
| `POST` | `/api/v1/worlds/{name}/restore/{snapshot}` | Roll back to a snapshot |
| `POST` | `/api/v1/worlds/{name}/migrate` | Stop → export → import → start on another host → cut over |
| `POST` | `/api/v1/worlds/{name}/restart` | Restart a world's container in place (§14D) |
| `GET`/`PUT` | `/api/v1/worlds/{name}/access` | Per-world whitelist toggle + ops list (§11) |
| `GET`/`PUT`/`DELETE` | `/api/v1/worlds/{name}/plugins/{id}/files[/{path}]` | View/edit/revert a plugin's config file(s) (§14D) |
| `GET` | `/api/v1/routes` | Live routing table for `folia-nexa-proxy` |
| `POST` | `/api/v1/plugins/stage` | Upload + validate a plugin jar against a staging clone |
| `POST` | `/api/v1/plugins/promote` | Promote a staged plugin into a world template |
| `POST` | `/api/v1/auth/login` | Operator login (dashboard/CLI), returns a session/API token |
| `GET`/`POST` | `/api/v1/users` | Manage operator accounts + roles (admin only, §11) |

---

## 11. Access Control: Operators & Players

Two different questions, two different mechanisms — don't conflate "who can run `worlds delete`" with "who can join the SMP":

### A. Operator access (the mgmt UI/API itself)

- Local accounts in mgmt's own state (`$SNAP_COMMON/mgmt/users.db`), passwords hashed with argon2, session cookies for the dashboard and long-lived API tokens for CLI/CI use — the CLI in §4/§17 authenticates the same way the dashboard does.
- Three roles, deliberately not more:
  - **admin** — everything, including trusting/draining hosts and managing other users.
  - **operator** — create/delete/snapshot/promote worlds and plugins, no host trust or user management.
  - **viewer** — read-only: dashboard, metrics, logs.
- OIDC/SSO federation is an explicit non-goal for v1 — it's real work that only pays off past single-operator scale, and local accounts + roles cover a homelab cluster fine. Noted here so it's a deliberate deferral, not an oversight.

### B. Player access (who can join, and what they can do in-game)

Don't build a bespoke ACL system — every world already needs a permissions plugin, so make that the single source of truth and have mgmt front it:

- **LuckPerms** (§14) as the permissions backend everywhere, backed by a shared MySQL/MariaDB instance so groups/tracks/permissions stay consistent across every world *and* the proxy — a player's rank follows them from `world-lobby` to `world-overworld` without per-world reconfiguration. **Implemented, with a correction from the original plan:** that MySQL instance is *not* scheduled as a `type: infra` world through mgmt — folia-nexa-node only knows how to run a Folia/Paper JVM (§9), not arbitrary services, so a database server isn't something the current node agent can run. It's provisioned as its **own snap** instead, `folia-nexa-db` (`db/`) — bundles MariaDB itself and bootstraps a dedicated database/user with a generated password on first start, so getting the shared backend running is `snap install` + `snap start`, not a multi-step manual container setup. (`configs/luckperms/provision-mysql.sh`, a plain-LXD-container alternative, still exists for operators who'd rather not add another snap.) Either way, the operator points mgmt at whatever's listening via `luckperms_mysql_*` settings; what mgmt automates is every LuckPerms-enabled world's `config.yml` staying in sync with that instance on every reconcile pass (`folia_mgmt/luckperms.py`), not the database's deployment. `folia-nexa-db` is deliberately its own snap, not bundled into `folia-nexa-mgmt`'s — worlds connect to it directly (LuckPerms plugin → MySQL wire protocol, never through mgmt's API), so a `snap refresh folia-nexa-mgmt` should never interrupt every running world's active DB connection. One real limitation either way: LuckPerms reads its storage backend at plugin load time, so a world whose config just changed needs a restart to actually pick it up — pushing the file doesn't force one.
- Mgmt's dashboard doesn't reimplement LuckPerms' editor — it deep-links to LuckPerms' own web permissions editor (pointed at the shared MySQL backend) for group/track management, and only adds the two things that are genuinely cluster-level concerns:
  - a network-wide whitelist toggle,
  - a per-world ops list.
- `GET/PUT /api/v1/worlds/{name}/access` is the API surface for both, both implemented: mgmt resolves each `ops` name to a UUID via the Mojang API and pushes `ops.json` straight into the running container over LXD's file-push API (no exec round trip needed for a plain file write); `whitelist_enabled=true` pushes `whitelist.json` mirroring the *same* network-wide Discord-approved set §11C's access gate already uses, rather than maintaining a second, separate per-world guest list that could drift out of sync — the toggle means "also enforce network approval at this world's Paper level," not "give this world its own guest list." A periodic reconcile pass (§5) keeps it current as approvals change, not just at toggle time. One real gap remains: nothing here templates `server.properties`' own `white-list` flag or sends an RCON command, so actually turning Paper's enforcement of the pushed file on/off is still a follow-up.

### C. Requesting access, via Discord

"Can this person join at all" is a network-wide question, not a per-world one, so it's enforced once, at the front door. **Implemented:** `folia-routes-sync` (§8C) doubles as the access gate rather than being a separate plugin — it already polls mgmt on a timer for the routing table, so polling `GET /api/v1/access-requests/approved-uuids` on the same cycle and denying `LoginEvent` for anyone not in that set costs nothing extra to run. This means v1 doesn't need the shared MySQL `network_access` table at all — mgmt's own SQLite is the source of truth, same as everything else it tracks. The gate is opt-in (`FOLIA_ACCESS_GATE_ENABLED`, default off) so a fresh install never locks the operator out by surprise. Moving to a MySQL-backed `network_access` table (e.g. if something other than this proxy plugin ever needs to check approval) is a future migration, not a v1 requirement.

Getting approved is a Discord OAuth2 flow, not an operator manually running `whitelist add`:

1. Player hits mgmt's public "Request Access" page → **Sign in with Discord** (standard OAuth2 authorization-code flow; mgmt is a registered Discord application with `identify` + `guilds.members.read` scopes) — or, if they'd rather stay in Discord, runs folia-nexa-bot's `/request-access <minecraft_username>` command instead (§16). Either path is a **one-time** step: it's the only way mgmt learns which Minecraft account belongs to that Discord user, and it's required even if the player already holds the auto-approve role described below — role-sync (below) only ever manages requests it already knows about, it never invents one for a Discord member it's never heard from.
2. `GET /api/v1/auth/discord/callback` (mgmt) exchanges the code, then calls Discord's API *with the player's own token* to confirm they're a member of the configured guild — no bot needed for this check.
3. Player links a Minecraft username once (resolved to a UUID via the Mojang API) — stored alongside their Discord ID.
4. Policy, set cluster-wide from the dashboard's "Discord role gate" card (Access Requests tab, §12) rather than a static setting: `enabled` + one `guild_id` + one `role_id` (`DiscordAccessGateConfig`, a singleton DB row — replaces what used to be `discord_guild_id`/`discord_auto_approve_role_id` env vars, so it can be changed without a redeploy). Approves immediately if the player holds that role at request time; otherwise the request lands as `pending` for an operator to approve/deny from the Access panel or — for mods who live in Discord — via the bot command above.
5. Approval makes the player's UUID show up in `GET /api/v1/access-requests/approved-uuids` on the gate's next poll (§8C), and pushes to any world with `whitelist_enabled` immediately rather than waiting for the next reconcile tick — every status-changing endpoint below does this now, not just the world-access toggle.

**Dynamic role-sync (implemented):** the one-shot decision at step 4 above used to be the whole story — nothing re-checked it afterward. It no longer is. `folia-nexa-bot` runs with the privileged Discord `members` gateway intent (Developer Portal setting, §17), which gives it a live, gateway-maintained cache of who currently holds the configured role at zero extra API cost. On every relevant `on_member_update` (someone gains or loses that one role) and on a 15-minute safety-net timer regardless, it POSTs the role's *complete current membership* to `POST /api/v1/access-requests/role-sync`, which reconciles `AccessRequest.status` against it: grants access to a known, currently-pending-or-revoked player who now holds the role; revokes a known, currently-approved player who no longer does. A revoked player isn't kicked from an active session — only future joins are blocked (v1 scope; actively disconnecting a live session was considered and deferred, since it needs new proxy-side work no other feature here has needed).

This reconciliation only ever touches `AccessRequest` rows flagged `auto_managed=True` (the model's default) — an operator's explicit `approve`/`deny` from the dashboard sets `auto_managed=False` on that row, which makes it permanently sticky against role-sync until a human acts on it again, the same "needs an explicit human re-decision" guarantee §11C already had for denied requests, now extended to cover automatic re-approval too. This is also how the **manual allowlist** works: the dashboard's "Manual allowlist" card (same tab) creates an `AccessRequest` row the normal way but with `auto_managed=False` from the start — a Minecraft-only entry, no Discord account involved at all, permanently exempt from role-sync, for admins/testers/anyone who shouldn't depend on holding the Discord role. It rides the exact same enforcement pipeline as everything else (proxy gate + per-world whitelist.json), so it's cluster-wide by construction, not a second, separate guest list.

A role-sync-revoked request gets a fourth `AccessRequestStatus` value, `revoked`, distinct from an operator's `denied` — same enforcement effect (excluded from `approved-uuids`/`whitelist.json`, both of which filter on `status == approved` explicitly), but a different *meaning* for anyone reading the Access panel: "lost the role" isn't "an admin said no," and conflating them would make a later dashboard "why is this person blocked" glance harder to answer at a glance.

API surface:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/auth/discord/callback` | OAuth2 redirect target; creates/updates the access request |
| `POST` | `/api/v1/access-requests` | Create/upsert a request (operator token — used by folia-nexa-bot's `/request-access`, §16, and the dashboard's manual-allowlist form) |
| `GET` | `/api/v1/access-requests` | List requests, filterable by status (operator/admin) |
| `GET` | `/api/v1/access-requests/approved-uuids` | Polled by the proxy's access gate (viewer-role token) |
| `POST` | `/api/v1/access-requests/{id}/approve` | Approve → picked up by the gate's next poll; marks the row `auto_managed=False` |
| `POST` | `/api/v1/access-requests/{id}/deny` | Deny, with an optional reason shown to the player; marks the row `auto_managed=False` |
| `GET`/`PUT` | `/api/v1/access-requests/discord-gate-config` | Read (viewer)/write (operator) the cluster-wide role-gate config — the dashboard card, and what folia-nexa-bot polls every 60s |
| `POST` | `/api/v1/access-requests/role-sync` | Called by folia-nexa-bot with the role's complete current membership; reconciles `auto_managed=True` rows accordingly |

---

## 12. Web Dashboard

Same spirit as the old plan's single-page dashboard, extended with the concepts above:

- **Hosts view:** list of trusted LXD hosts, capacity bars, "Add host" flow (generates a join token for `folia-host-join`, §4).
- **Worlds view:** table of all worlds with type, host, TPS, players, phase; "Add world" (pick type/template, resource request, placement labels); per-world drain/snapshot/restore/migrate/restart actions.
- **Access panel:** per-world whitelist/ops toggle (§11B), deep link to LuckPerms' web editor; operator user/role management (§11A, admin only).
- **Plugin config modal (§14D):** per-world, per-plugin file browser + text editor, reachable from a "Configs" button on the Worlds view — view/edit any file under an installed plugin's folder, with a "restart to apply" action alongside it.
- **Staging panel:** unchanged concept from the old plan (§13 below), now backed by LXD copy-from-snapshot instead of shell scripts.

---

## 13. Staging & Promotion Workflow

Staging a plugin change is now just LXD snapshot + copy, orchestrated by mgmt instead of a shell script talking to one hardcoded container name:

1. `POST /api/v1/worlds/world-overworld/snapshot` → `pre-plugin-<ts>`.
2. mgmt calls `lxc copy node-a:world-overworld/pre-plugin-<ts> node-a:world-overworld-staging -p folia-e-core` (declared as a `type: staging` world so it inherits `snapshot_policy: none` and is excluded from `folia-nexa-proxy`'s route table by default).
3. Plugin jar validated (`plugin.yml` must declare `folia-supported: true`, same guard as the old plan) and pushed into the staging container's plugin dir.
4. Operator connects to the staging world directly (mgmt can optionally add it to `/api/v1/routes` with `default: false` for a manual-connect test address) and validates gameplay.
5. `POST /api/v1/plugins/promote` — pushes the validated jar into the source world's template, restarts `world-overworld`'s node agent, deletes the staging container.

Migration (moving a sticky world to a different host) is **implemented**, via `POST /worlds/{name}/migrate?target_host=<name>`: stop the container, export it as an LXD backup tarball, import that on the target host, start it there, then delete the source — a brief outage, not a live migration (raw LXD migration is websocket-based and considerably more complex to get right without a live daemon to validate against, so this deliberately uses the plain-HTTP backup export/import API instead). Capacity on the target is checked before attempting anything; a failed migration leaves the world exactly where it was — the source is only deleted after a successful start on the target.

---

## 14. Curated Plugin Matrix (Folia-Supported)

All plugins must target Folia's `RegionScheduler` and `GlobalRegionScheduler` to prevent single-thread bottlenecks:

| Category | Plugin Name | Role & Feature |
| --- | --- | --- |
| **Permissions** | `LuckPerms` | Shared permissions/groups/tracks backend (MySQL), synced across every world and the proxy — see §11B. |
| **Chat/Discord Bridge** | `DiscordSRV` (Folia-compatible) | In-world chat ↔ Discord channel relay, join/quit and death announcements. |
| **Land Claims** | `HuskClaims` / `CrashClaim` | Multi-threaded land claims, trust flags, anti-griefing, and nation boundaries. |
| **Economy & Trade** | `Vault-Unlocked` + `AxAuctions` | Distributed player auctions, safe GUI trading (`TradeSystem`), and regional shops. |
| **RPG & Skills** | `AuraSkills` | RPG skill trees (Mining, Combat, Agility), mana, crit stats, and ability unlocks. |
| **Custom Gear** | `ItemsAdder` (Folia branch) | Custom 3D tools, weapons, furniture, and HUD overlays via automated resource packs. |
| **Custom Bosses** | `MythicMobs` (v5.6+ Folia build) | Custom scripted world bosses, phased mob attacks, and unique drop tables. |
| **Navigation** | `HuskHomes` + `HuskPortals` | Asynchronous teleportation (`/tpa`, `/home`) and cross-server dimension gates. |
| **Social & Voice** | `SimpleVoiceChat` (Folia addon) | Positional 3D voice chat and dynamic proximity radio channels. |
| **Vanity & Lobby** | `FancyNpcs` + `FancyHolograms` | High-performance display entity NPCs and 3D leaderboards without armor-stand lag. |
| **Diagnostics** | `Spark` (Folia build) | Live profiling of individual region tick rates, memory leaks, and CPU load. |
| **Web Map** | `BlueMap` | Interactive asynchronous 3D isometric world map rendered in the browser. |

### 14A. Catalog Implementation

The matrix above is a design reference; the thing mgmt and worlds actually use is `mgmt/src/folia_mgmt/catalog.yaml`, a flat list of entries (`id`, `category`, `source: external|in-house`, `version`, `download_url`, `sha256`, `homepage`, `verified`, `notes`) checked into this repo and bundled with the `folia-nexa-mgmt` snap. It's just an index — `download_url` can point at a vetted external plugin's own release artifact (GitHub releases, a project's own CDN) or at an in-house plugin published from a separate repository; the catalog doesn't host or build anything itself.

- **Extending without a new mgmt release**: an operator can add or override entries by dropping a `plugin-catalog-override.yaml` (same schema, a list of entries) in mgmt's state dir (`$SNAP_COMMON` under the snap). Entries there are merged over the bundled ones by `id` — useful for pinning a different version, filling in a `download_url` for one of the placeholder entries, or adding a private in-house plugin. See `plugin_catalog.py`.
- **API**: `GET /api/v1/plugins` (list, optional `?category=`) and `GET /api/v1/plugins/{id}` (auth: any logged-in role) expose the merged catalog. `POST /api/v1/worlds` validates every id in a world's `plugins` list against the catalog and 400s on anything unknown — no more plugin names that only fail at the JVM, at boot, long after world creation. `GET /api/v1/worlds/{name}/plugins-manifest` is deliberately unauthenticated (consistent with node's zero-credential design, §9) and generates a world's manifest live from `world.plugins` + the catalog, skipping (with a log warning, not a 500) any entry whose `download_url` is still a placeholder.
- **CLI**: `folia-nexa-mgmt plugins list [--category]` and `plugins show <id>` browse the catalog; `worlds create --plugin <id>` (repeatable) takes catalog ids.
- **Dashboard**: the "Plugins" tab lists the catalog (id, category, source, version, verified/unverified, homepage); the "Declare a world" form has a checkbox picker populated from the same catalog instead of free text.
- Declaring any plugins requires `FOLIA_MGMT_PUBLIC_URL` to be set (the address the world's `plugins-manifest-url` points back at) — enforced at world-creation time.
- **Writing a new in-house entry?** `docs/plugin-dev/` is a three-part how-to series (Ubuntu dev environment → Folia-safe plugin architecture → submitting a catalog entry for review) aimed at someone who's never built a Minecraft server plugin before.

### 14B. The Lobby as a Hub

A `lobby`-type world is where players land first and pick which game/world to join next — not just another entry in the world list. Two pieces make that actually work:

- **It's the default landing point.** `GET /api/v1/routes` (§7) flags exactly one route `default: true`, and `_pick_default` in `routers/routes.py` prefers a running `lobby` world over a running `overworld` — so as soon as a cluster declares a lobby, new connections land there automatically, no proxy config change needed. Multiple lobby worlds are unlikely (a hub is meant to be the one shared front door) but if there ever are, the pick is deterministic (lowest name), not row-order-dependent.
- **It's where players choose a game.** The proxy already knows every running world by name — `folia-routes-sync` registers each one as a Velocity backend server as soon as mgmt reports it `running` (§7). That means the zero-plugin path already works: any player connected through the proxy can run `/server <world-name>` (Velocity's built-in command) to jump to any other running world, tab-completed from the live registration list. For a friendlier in-game menu instead of a raw command, the catalog's `ServerSelector` entry (category `lobby`) is a Paper plugin installed *on the lobby world itself* — not the proxy — that gives players a compass/GUI and switches them over the standard BungeeCord/Velocity plugin-messaging channel, so it needs no `folia-nexa-proxy` changes either. Its per-entry `id:` in `config.yml` should be set to the target world's name (the same string mgmt uses for `world.name` and the proxy uses for the backend server name) — see `docs/game-master-howto.md` for a worked example.

### 14C. Data Pack Support

Vanilla data packs (JSON-defined recipe/loot-table/advancement/function
tweaks — no jar, no Bukkit/Paper plugin loader involved — e.g. the
"Matcha Flavoured" gameplay-tweaks pack) are a different content type
from §14A's plugins, but follow the exact same catalog → manifest →
node-fetch shape, in a second, parallel instance of it:

- **Catalog**: `mgmt/src/folia_mgmt/datapacks.yaml` (`DatapackEntry`:
  `id`, `category`, `source`, `version`, `download_url`, `sha256`,
  `homepage`, `verified`, `notes`), loaded/merged with an operator
  override file (`datapack-catalog-override.yaml` in mgmt's state dir)
  the same way `plugin_catalog.py` does — see `datapack_catalog.py`.
  Kept as a second catalog file/module rather than folded into
  `PluginEntry`/`catalog.yaml` because it stages to a genuinely different
  place on disk (below).
- **API**: `GET /api/v1/datapacks` (+ `/{id}`), and
  `GET /api/v1/worlds/{name}/datapacks-manifest` — same
  validate-at-create-time, unauthenticated-manifest, skip-unresolved-
  entries-with-a-warning design as the plugin manifest. `World.datapacks`
  is validated against the catalog at `POST /api/v1/worlds` time, same as
  `World.plugins`.
- **CLI**: `folia-nexa-mgmt datapacks list [--category]` / `datapacks show
  <id>`; `worlds create --datapack <id>` (repeatable).
- **Dashboard**: a "Data Packs" tab mirroring "Plugins", and a second
  checkbox picker in "Declare a world".
- **Staging (the one real divergence from plugins)**: `folia-nexa-node`
  places each downloaded entry under
  `<world_dir>/<level-name>/datapacks/<id>.zip` — a world *save's*
  `datapacks/` folder, not the server root's `plugins/` — since data
  packs are read by the vanilla/Paper world-loading code, not a plugin
  loader. This codebase never templates `server.properties` (the Folia/
  Paper server generates its own save folder on first boot), so staging
  assumes the vanilla default level-name (`world` — see `LEVEL_NAME` in
  `node/src/folia_node/staging.py`); a world running a non-default
  level-name won't get its data packs staged to the right place. Staging
  before first boot works cleanly because data packs apply at world
  generation — no live-server `/reload` or restart-after-copy step is
  needed the way it would be for a plugin hot-swap.
- Declaring any datapacks requires `FOLIA_MGMT_PUBLIC_URL`, same as
  plugins, enforced at world-creation time.
- Not enforced (a manual check, same posture as the minigame-plugin
  Folia-compatibility caveat in §14A): a data pack's `pack.mcmeta`
  `pack_format`/`min_format`/`max_format` against the world's actual
  Minecraft version. Mismatches fail at world load, not at
  `worlds create` time.

### 14D. Plugin Config File Editing

§14A gets a plugin *installed* on a world; nothing before this let an
operator see or change what's *inside* the plugin's own config once it's
running — every plugin only writes its own `config.yml` (and whatever
else — lang files, sub-configs) after its own first boot, and the only
prior precedent, `luckperms.py`, pushes a config mgmt itself templates
from DB settings (§11B), not something an operator edits freely.

- **Storage**: `WorldPluginConfigFile` (`models.py`) — one row per
  (world, plugin, relative file path) an operator has edited, holding the
  raw content as the source of truth. Absence of a row means "defer to
  whatever the plugin itself wrote"; deleting a row reverts to that — it
  never deletes the live file, since mgmt has no delete-file LXD call and
  removing a plugin's own generated file could break it.
- **Reading**: an mgmt-stored override always wins; otherwise mgmt reads
  the live file straight out of the world's container over LXD's file API
  (`LXDClient.read_file`/`list_files`, the read/list counterparts to the
  existing `push_file` — PLAN.md §6's snapshot/restore already use the
  same instance API family). UTF-8 decode failure is reported as
  `is_binary: true` rather than an error, since a plugin folder can
  contain non-text files (data/db files, images) the browser shouldn't
  try to render as editable text.
- **Writing**: `PUT .../files/{path}` upserts the DB row and attempts an
  immediate live `push_file`; on failure (world unreachable) the edit is
  still saved and retried by `scheduler.py`'s `sync_plugin_config_files`
  on every reconcile tick — the exact same "overwrite unconditionally,
  swallow `LXDError`, retry next tick" pattern `sync_luckperms_configs`
  already established, generalized from one hardcoded plugin to any
  plugin in a world's declared list. That reconcile loop also re-checks
  `row.plugin_id in world.plugins` on every tick, not just at request
  time — a plugin removed from a world (`PATCH /{name}`) stops having its
  stale override re-pushed forever, matching what the read/write
  endpoints already enforce via `_require_declared_plugin`.
- **Managed plugins are off-limits, reads included**: `LuckPerms` and
  `FoliaNexaStats` are the two exceptions to "any plugin in a world's
  declared list" above — `luckperms.py`/`folianexa_stats.py` already
  render and push their `config.yml` from live cluster secrets (the
  shared MySQL password, an operator-scoped mgmt API token), at exactly
  the container paths this browser would otherwise serve.
  `plugin_files.MANAGED_PLUGIN_IDS` is checked in all four
  `plugin_config.py` handlers (`_require_not_managed`, right after
  `_require_declared_plugin`) and rejects with 403 — including `GET`,
  since a `viewer`-role token (e.g. the proxy's own service account,
  deliberately provisioned viewer-only) reading either secret back out
  would be a viewer→operator privilege escalation, not just an unwanted
  write. `sync_plugin_config_files` independently skips any
  `WorldPluginConfigFile` row whose `plugin_id` is managed, as defense in
  depth against a row that predates this check — without it, that
  reconcile loop (which runs *after* `sync_luckperms_configs`/
  `sync_stats_configs` in `reconcile()`) would silently and permanently
  overwrite the real config with the stale override on every tick. The
  dashboard's plugin picker filters `MANAGED_PLUGIN_IDS` out of
  `world.plugins` client-side too, purely as UX — the actual boundary is
  server-side and enforced regardless of what the picker offers.
- **File browser, not just `config.yml`**: `GET .../files` recursively
  walks a plugin's folder (`plugin_files.py`, capped at depth 6 / 500
  files) merging what's live in the container with any mgmt overrides, so
  plugins with lang files or nested sub-configs are all reachable, not
  just one fixed filename.
- **Path traversal is rejected, not just the plugin id**: the `{path}`
  segment of `GET`/`PUT`/`DELETE .../files/{path}` is a client-controlled
  catch-all — `_require_declared_plugin` alone only validates `plugin_id`,
  leaving `path` free to walk out of the plugin's own folder with `../`
  segments. `routers/plugin_config.py`'s `_safe_relative_path` normalizes
  the path (`posixpath.normpath`) and 400s anything that still escapes
  upward or is absolute *before* it's ever joined onto `plugin_root(...)`
  and handed to `LXDClient.read_file`/`push_file`/the override lookup —
  applied in all three handlers, not just the read path.
- **Applying an edit**: most plugins only read their config at boot, so
  an edit sits inert until the world restarts. `POST
  /api/v1/worlds/{name}/restart` (added alongside `/stop`/`/start`/
  `PATCH /{name}` for the same "edit now, apply on restart" world-update
  flow) gives an operator a manual way to apply one on demand — this
  feature doesn't need its own restart endpoint, it just reuses that one.
- **Dashboard**: a "Plugin configs" button per world (Worlds tab, next to
  "Configure"/"Restart"/etc.) opens the app's first modal — a plugin
  picker, a file tree, and a text editor, built from the same
  `api()`/`escapeHtml()` conventions and `.card`/`.btn`/`.hint` classes
  every other tab already uses. Its own "Restart world to apply" button
  reports into the modal's own `#config-modal-error`, not the page-level
  `#worlds-error` (hidden behind the modal overlay) — same pattern the
  inline world-edit panel's `restartWorldEdit()` already established. See
  `static/index.html`'s plugin config modal functions.
- **CLI**: `folia-nexa-mgmt worlds plugin-config list|show|set|revert
  <world> <plugin-id> [path]`, and `worlds restart <world>`.
- Only plugins already in a world's declared `plugins` list are editable
  (404 otherwise), and never `LuckPerms`/`FoliaNexaStats` (403 — see
  above) — this isn't a general container file browser, it's scoped to
  what §14A already lets a world run, minus what mgmt manages itself.

---

## 15. Sample MythicMobs & ItemsAdder Configurations

### A. Custom Weapon: "Hyperion Blade" (`plugins/ItemsAdder/data/items/weapons.yml`)

```yaml
info:
  namespace: custom_weapons
items:
  hyperion_blade:
    enabled: true
    display_name: "&6&lHyperion Blade"
    permission: custom_weapons.hyperion
    lore:
      - "&7An ancient hyper-dimensional broadsword."
      - ""
      - "&6Ability: Shadow Warp &eRIGHT CLICK"
      - "&7Teleports you 8 blocks ahead and creates a"
      - "&7kinetic shockwave dealing &c+150 Damage&7."
      - ""
      - "&c+18 Attack Damage"
      - "&9+25% Critical Strike Chance"
    resource:
      material: NETHERITE_SWORD
      generate: false
      model_path: item/hyperion_blade
    events:
      interact:
        right:
          play_sound:
            name: entity.enderman.teleport
            volume: 1.0
            pitch: 1.2
          particles:
            name: EXPLOSION_NORMAL
            count: 15
```

### B. Custom World Boss: "Corrupted Void Colossus" (`plugins/MythicMobs/Mobs/VoidColossus.yml`)

```yaml
VoidColossus:
  Type: WITHER_SKELETON
  Display: '&4&lCorrupted Void Colossus &6[Lv. 100]'
  Health: 3500
  Damage: 24
  Armor: 15
  Faction: VoidInvaders
  Options:
    AlwaysShowName: true
    MovementSpeed: 0.32
    PreventOtherDrops: true
    KnockbackResistance: 1.0
  AIGoalSelectors:
    - 0 clear
    - 1 meleeattack
    - 2 randomstroll
  AITargetSelectors:
    - 0 clear
    - 1 players
  Skills:
    # Phase 1: Ground Slam AOE
    - skill{s=ColossusGroundSlam} @self ~onTimer:160 ?health{gt=1750}
    # Phase 2: Void Rift Summoning (under 50% HP)
    - message{m="&4&lVoid Colossus:&c The void consumes your reality!"} @PIR{r=30} ~onDamaged ?health{lte=1750} ~once
    - skill{s=SummonVoidRifts} @self ~onTimer:200 ?health{lte=1750}
    # Death Drop Table
    - drop{table=ColossusDropTable} @self ~onDeath

ColossusGroundSlam:
  Skills:
    - message{m="&c&lWatch out! &7The Colossus slams the earth!"} @PIR{r=20}
    - effect:particles{p=block;m=OBSIDIAN;a=100;vs=1.5;hs=1.5} @self
    - damage{a=40} @PIR{r=8}
    - throw{velocity=12;velocityY=8} @PIR{r=8}

SummonVoidRifts:
  Skills:
    - effect:particles{p=PORTAL;a=200;vs=2.0;hs=2.0} @self
    - potion{type=WITHER;duration=100;level=2} @PIR{r=15}
    - summon{type=WITHER_SKELETON;amount=3;radius=6} @self
```

---

## 16. Future Expansion: Public Community & Analytics Portal

**Partially superseded by §7A**, which shipped a deliberately scoped-down
v1 of this vision (SQLite instead of Postgres/ClickHouse, no Redis, no
WebSocket live-push, a plain static `portal/` instead of a Next.js/Astro
`world (type: portal)`). Read §7A for what actually exists today; this
section remains the aspirational full vision beyond that v1 — 2D face
avatars did make it into v1, though self-hosted rather than via a
third-party CDN as originally planned here (`GET
/api/v1/public/players/{uuid}/avatar`, `mgmt/src/folia_mgmt/avatar.py` —
crafatar.com, the CDN this section originally named, had a real
multi-hour outage on 2026-08-16 that broke every avatar on the portal
simultaneously, which is what prompted rendering them from Mojang skin
data directly instead). The rest below (live WebSocket presence, a real
event-sourced Postgres/ClickHouse store, 3D avatar rendering, the portal
running as an LXD-scheduled world of its own) is still unbuilt.

Unaffected by the orchestration refactor above — still an asynchronous telemetry portal fed by proxy/world events, now simply pointed at whichever containers the scheduler happens to be running:

```
       ┌──────────────────┐       ┌──────────────────────────┐
       │   folia-nexa-proxy  │       │   worlds (any host)       │
       │ (Velocity Redis)  │       │ (HuskSync MySQL)          │
       └────────┬─────────┘       └────────┬──────────────────┘
                │ Events (Join/Quit/Stats) │
                ▼                          ▼
       ┌─────────────────────────────────────────────┐
       │     Telemetry Ingestion Service / Redis     │
       └──────────────────────┬──────────────────────┘
                              │
                              ▼
       ┌─────────────────────────────────────────────┐
       │           PostgreSQL / ClickHouse           │
       │    • Player Registry (UUID, Aliases)        │
       │    • Session History & Playtime Windows     │
       │    • Global & Regional Statistics           │
       └──────────────────────┬──────────────────────┘
                              │
                              ▼
       ┌─────────────────────────────────────────────┐
       │ world (type: portal), scheduled like any     │
       │ other world                                  │
       │ • Next.js / Astro Static-Edge Front End      │
       │ • Public REST & WebSocket Live APIs          │
       │ • Crafatar / Minotar 3D Avatar Rendering     │
       └─────────────────────────────────────────────┘
```

### Core Features

1. **Live Network Pulse:** 3D avatar gallery of currently active players, online counts, current server location, and regional tick status.
2. **Historical Registry:** Searchable directory by username or UUID, lifetime playtime counters across Overworld vs. Nether vs. End, and join history.
3. **Leaderboard Tracking:** Top `AuraSkills` power levels, richest merchant rankings from `AxAuctions`/`Vault-Unlocked`, and blocks mined.
4. **Player Profile Cards (`/player/[uuid]`):** 3D skin renders, GitHub-style 365-day playtime heatmaps, and public settlement badges from `HuskClaims`.

### Discord Bot (`folia-nexa-bot`, `bot/`)

**Implemented**, as a Python package (`discord.py` for the gateway/heartbeat/reconnect protocol — deliberately not hand-rolled; see the module docstring in `bot/src/folia_bot/bot.py` for why) with four slash commands, all backed by mgmt's REST API rather than any direct DB/LXD access:

- `/status` — embed of currently declared worlds and their phase/host, from `GET /api/v1/worlds`. Player counts and TPS aren't in that response (nothing in this codebase measures them yet), so the embed says so rather than fabricating numbers.
- `/who [world]` — who's online right now and which world they're in, from `GET /api/v1/public/worlds` (§7A's presence API, the same endpoint the portal's "who's online" page reads).
- `/leaderboard [stat]` — top players by a stat (kills, deaths, blocks mined, playtime, AuraSkills power level, AxAuctions wealth), from `GET /api/v1/public/leaderboards` (§7A's leaderboard API, same endpoint the portal reads). No longer the stub this section originally described — §7A shipped the SQLite-backed analytics store this needed. Data may still be genuinely empty until a real stats-reporting plugin is deployed (catalog id `FoliaNexaStats` is a placeholder pending its first release, per CLAUDE.md); the embed says "no data yet" rather than fabricating numbers.
- `/request-access <minecraft_username>` — the in-Discord counterpart to §11C's web OAuth flow, via a new `POST /api/v1/access-requests` endpoint (operator-role token). Auto-approves locally from the inviting member's Discord roles (`FOLIA_BOT_AUTO_APPROVE_ROLE_ID`) without a round trip to Discord's API, since the bot already has that information from the interaction. This is a **one-time** step, still required even once the Discord role-sync below is enabled and even if the player already holds the gate role — it's the only way mgmt learns which Minecraft account is theirs; role-sync never creates a request on its own for a Discord member mgmt hasn't heard from yet.

Beyond the four slash commands, a background presence-join announcer (`poll_presence`, `bot/src/folia_bot/presence.py`) polls `GET /api/v1/public/worlds` on a timer and diffs consecutive snapshots — mgmt has no join/quit event stream of its own, folia-routes-sync only ever reports a full per-world snapshot on its existing 5s poll cycle, never a delta — posting one line to a configured channel (`FOLIA_BOT_PRESENCE_CHANNEL_ID`) for every player whose world changed since the last poll (a fresh join, or a switch between worlds; no leave/quit announcement). Off by default (unset channel id = the loop never starts).

Beyond the three slash commands, the bot also drives §11C's **dynamic role-sync**: with the privileged `members` gateway intent enabled (both in code and, one-time, in the Discord Developer Portal — the bot fails `PrivilegedIntentsRequired` at connect otherwise), it keeps a live cache of who holds mgmt's dashboard-configured gate role and pushes the complete current membership to `POST /api/v1/access-requests/role-sync` on every relevant `on_member_update` plus a 15-minute safety-net timer regardless. The gate's `enabled`/`guild_id`/`role_id` themselves aren't bot config at all — they live in mgmt's DB (`DiscordAccessGateConfig`), edited from the dashboard, and the bot polls `GET /api/v1/access-requests/discord-gate-config` every 60s so a toggle there takes effect without a bot restart.

Join announcements are now handled by the presence-join announcer above, cluster-wide and world-aware by construction — the same rationale §16's chat bridge already used to avoid installing `DiscordSRV` (§14) per-world: that plugin runs on one Paper server at a time (catalog.yaml's own notes: install it on one hub world, not every world, or duplicate relays result), so a proxy-wide feature is a better fit here than a per-world plugin. `DiscordSRV` remains cataloged for operators who want its other features (quit/death event relay, admin commands from Discord) on a single hub world regardless.

`discord.Client`/`CommandTree` construction and command registration were verified to build correctly against the real `discord.py` API from the start, and everything in `embeds.py`/`access.py`/`mgmt_client.py` (the actual bot-specific logic, including the role-sync reconcile functions) is unit-tested. **The bot's actual gateway connection is now also confirmed live** (2026-08-16): deployed to a real production host with a real bot token and the privileged members intent enabled, it connected successfully with no `PrivilegedIntentsRequired` crash and began polling the gate config on its own right after `on_ready`. Not yet observed live: a real `/request-access` invocation by an actual Discord member, or a real `on_member_update` event actually firing the role-sync POST — only the connection itself and the periodic config poll have been watched directly so far.

---

## 17. Step-by-Step Rollout & Deployment Sequence

### Phase 1: Prepare the first LXD host

1. Deploy Ubuntu Server 24.04 LTS, initialize ZFS storage backing for LXD:
   ```bash
   zpool create -f default /dev/nvme0n1
   zfs set compression=zstd default
   ```
2. Initialize LXD with ZFS backing.

The remote API, the `folia` project, and its quotas no longer need manual setup — `folia-host-join` (§4) does all of that in Phase 3, driven by the join token mgmt issues.

### Phase 2: Build snaps

1. Build `folia-nexa-mgmt`, `folia-nexa-node`, and `folia-nexa-proxy` with `snapcraft --use-lxd`.
2. Publish to a private snap store channel or push `.snap` files directly to hosts.
3. Build (or point mgmt at) a base LXD image with `folia-nexa-node` preinstalled, so container launch doesn't need a post-boot snap install step.

### Phase 3: Bring up the control plane

1. Install `folia-nexa-mgmt` on its own container/VM (it needs almost no resources — 1 E-core class host is plenty).
2. Create the first operator account (`admin` role) and log in to the dashboard (§11A).
3. Issue a join token and run `folia-host-join` on the host from Phase 1 (§4):
   ```bash
   folia-nexa-mgmt hosts create-join-token
   sudo ./tools/folia-host-join.sh --mgmt-url https://mgmt.internal:8443 --join-token <token> --address 10.0.1.11
   ```
4. Confirm `GET /api/v1/hosts` shows `node-a` online with correct capacity.
5. Optionally, create per-workload LXD profiles inside the `folia` project the join created (e.g. `folia-p-core`, `folia-e-core`) mirroring the CPU pinning ranges from the original hardware table — these become `placement.labels` targets for the scheduler, not fixed container assignments:
   ```bash
   lxc profile create folia-p-core --project folia
   lxc profile set folia-p-core limits.cpu "0-5" --project folia
   lxc profile create folia-e-core --project folia
   lxc profile set folia-e-core limits.cpu "6-13" --project folia
   ```

### Phase 4: Declare worlds

1. Through the dashboard or API, declare the initial world set:
   ```bash
   folia-nexa-mgmt worlds create world-overworld --type overworld --cpu 6 --memory 12GB --labels cpu_type=p-core
   folia-nexa-mgmt worlds create world-nether    --type nether    --cpu 2 --memory 3GB  --labels cpu_type=e-core
   folia-nexa-mgmt worlds create world-lobby     --type lobby     --cpu 2 --memory 3GB  --labels cpu_type=e-core
   ```
2. Watch the scheduler place them; confirm `GET /api/v1/routes` and `folia-nexa-proxy` pick them up automatically.
3. Pre-generate the overworld with `chunky start world 10000` via `lxc exec node-a:world-overworld -- ...` once it's running.

### Phase 5: Scale out

Adding a second host is Phase 1 steps 1–2 plus one `folia-host-join` run on the new machine. No topology diagram to redraw — the next `worlds create` (a minigame instance, a second nether shard, whatever) just has more capacity to land on.
