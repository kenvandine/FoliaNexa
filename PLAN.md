# Folia Multi-World SMP Cluster: Architecture & Implementation Plan (v2)

**Control Plane:** `folia-smp-mgmt` (snap) — orchestrator, scheduler, REST API, web dashboard
**Compute Agent:** `folia-smp-node` (snap) — runs inside every world's LXD container, runs the JVM, reports health
**Edge:** `velocity-proxy` (snap) — public entry point, routing table synced live from mgmt
**Substrate:** One or more standalone LXD hosts, each trusted individually by `folia-smp-mgmt` over the LXD remote API
**Application Packaging:** Snaps with `systemd` daemon supervision throughout

> Supersedes the static-topology plan in `PLAN.md.old`. That version hardcoded four fixed containers (`folia-smp`, `folia-nether`, `hub-lobby`, `edge-proxy`) pinned to specific cores on one box. This version replaces the fixed topology with a scheduler: any number of LXD hosts contribute capacity, and `folia-smp-mgmt` decides which worlds (overworld, nether, end, lobby, minigames, ephemeral staging, …) run where, and moves them around as capacity changes.

---

## 1. Architecture Overview

Three roles, cleanly separated:

- **`folia-smp-mgmt`** never runs a Minecraft process itself. It holds cluster state (which hosts exist, which worlds should exist, where each is currently placed), talks to each LXD host's remote API to create/destroy/snapshot containers, and exposes a REST API + web dashboard for operators.
- **`folia-smp-node`** is baked into (or installed at first boot of) every world's container. It never talks to the scheduler to ask "what should I run" — its instance already knows, because `folia-smp-mgmt` wrote that assignment into the container's own LXD instance config at creation time. The node agent's job is: fetch the jar + plugins, run the JVM, expose local health/TPS metrics, restart on crash.
- **`velocity-proxy`** is the single public-facing port. It doesn't hardcode a server list — it polls `folia-smp-mgmt`'s routing API and rebuilds its backend list as worlds come and go.

```
                                   [ Public Internet ]
                                            │
                                    (Port 25565/TCP)
                                            ▼
                          ┌──────────────────────────────────┐
                          │  velocity-proxy (dynamic routes)  │
                          └──────────────────┬─────────────────┘
                                            │  polls /api/v1/routes
                                            ▼
┌───────────────────────────────────────────────────────────────────────┐
│                          folia-smp-mgmt                                 │
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
│ │ folia-smp-node     │ │   │ │ folia-smp-node     │ │   │ │ folia-smp-node     │ │
│ └───────────────────┘ │   │ └───────────────────┘ │   │ └───────────────────┘ │
│ ┌───────────────────┐ │   │                        │   │                        │
│ │ world-nether       │ │   │                        │   │                        │
│ │ folia-smp-node     │ │   │                        │   │                        │
│ └───────────────────┘ │   │                        │   │                        │
└──────────────────────┘   └──────────────────────┘   └──────────────────────┘
```

`node-a`, `node-b`, `node-c` can be one box (today: the Core Ultra 5 235T) or many. Adding capacity is "trust another LXD host," not "edit a topology diagram."

---

## 2. Core Concepts & Data Model

### Host

A **Host** is a standalone LXD daemon that `folia-smp-mgmt` has been granted restricted, project-scoped access to. Mgmt tracks:

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

`type: infra` exists in the schema for a Folia/Paper-based shared dependency a scheduler-placed world could point at, excluded from `velocity-proxy`'s route table by default. In practice, neither shared dependency this project actually needed turned out to fit that mold: the MySQL/MariaDB instance backing LuckPerms (§11B) and `folia-discord-bridge` (§16) both run things folia-smp-node can't (a database server; a standalone Discord bot process) and are provisioned/installed directly rather than scheduled as worlds. `type: infra` is left in place for a future case that's actually a Folia/Paper process, but has no real user yet.

### Reconcile loop

`folia-smp-mgmt` is a controller: it compares desired world list against actual LXD state on every trusted host, and acts on drift — create a container for a pending world, restart a crashed one, tear down a deleted one. Same shape as any k8s-style controller, scaled down to this problem.

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

# Generate a one-time trust token for folia-smp-mgmt to consume
lxc config trust add --name folia-smp-mgmt --restricted --projects folia
# -> prints a one-time token
```

**From `folia-smp-mgmt`'s dashboard or CLI:**
```bash
folia-smp-mgmt hosts add node-a --address 10.0.1.11:8443 --token <one-time-token>
```

This performs the standard LXD trust exchange (mTLS, client cert generated and stored under `$SNAP_COMMON/mgmt/certs/`) and records the host. From this point mgmt's certificate is **restricted to the `folia` project** — it cannot see or touch any other project, storage pool, or host-level setting on that machine, even if the mgmt host itself is compromised. Blast radius of a leaked mgmt credential is "the folia project's containers on that one host," never the host itself or other tenants.

**Network requirement:** the LXD API port (8443) must be reachable from `folia-smp-mgmt`, and must **not** be exposed to the public internet — put it on a private management VLAN or a WireGuard mesh between mgmt and every host. This is the one hard networking requirement multi-host introduces; single-host deployments can just bind it to loopback/private bridge.

A host is **not required to be dedicated** to Folia — the project quota is the isolation boundary. A shared LXD box can host a `folia` project alongside unrelated projects as long as its quota reflects real spare capacity.

---

## 4. Automated Host Enrollment (`folia-host-join`)

Manually running the trust exchange from §3 on both sides doesn't scale past "the one box I'm sitting at." `tools/folia-host-join.sh` (in this repo) automates the entire host side of it down to one command, given the mgmt URL and a short-lived join token.

Two distinct tokens are involved, and it's worth being precise about what each one is for:

| Token | Issued by | Lifetime | Proves |
| --- | --- | --- | --- |
| **Join token** | `folia-smp-mgmt` (admin requests it via dashboard/CLI) | Short-lived (default 15m), single-use | "Whoever holds this is authorized to enroll one new host into *this* cluster" — the control that stops a random machine from adding itself as compute capacity. |
| **LXD trust token** | The host's own LXD daemon, generated by the script | Single-use, consumed immediately | Standard LXD mechanism that lets one specific client certificate (mgmt's) become permanently trusted by that host, scoped to the `folia` project. |

Flow:

1. Admin: `folia-smp-mgmt hosts create-join-token` (or the dashboard's "Add Host" button) → prints a join token.
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

`--skip-enroll` stops after generating the LXD trust token and prints it instead of calling mgmt — useful for finishing the trust exchange manually, or for exercising the host-side steps before `folia-smp-mgmt` itself exists.

> **Status:** the script is real and safe to run today for the LXD-prep steps (project/quota/https-address/trust-token). The final `POST /api/v1/hosts/enroll` call will fail until `folia-smp-mgmt`'s API (§10) is actually implemented — the script targets that contract now so nothing has to change on the host side once mgmt catches up.

---

## 5. Scheduler

### Placement

On each reconcile pass, for every world in `phase: pending`:

1. Filter hosts to those matching the world's `placement.labels` (if any) and `status: online`.
2. Filter to hosts with enough *unallocated* capacity (`capacity - sum(resources of worlds already placed there)`) for the world's `resources` request.
3. Pick the host with the most free capacity after placement (bin-pack the fullest-fit-remaining, not first-fit — keeps headroom spread evenly rather than stacking everything on one box).
4. Call the host's LXD API: launch a container from the base image, apply CPU/memory limits, write world assignment into instance config (see §9), apply the world's `snapshot_policy`.
5. Mark the world `phase: provisioning` until `folia-smp-node` reports itself healthy, then `phase: running`.

### World lifecycle states

`pending → provisioning → running → (crashed → restarting) → draining → deleted`

- **crashed**: node agent's local health endpoint stops responding or JVM exits non-zero; mgmt restarts the container (not a fresh reschedule — data and identity stay put).
- **draining**: operator-initiated (host maintenance, decommission). Mgmt snapshots the world, and either leaves it stopped or migrates it (§13) to another host with capacity.

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

---

## 7. Networking & Edge Proxy

Each host's `folia` project containers sit on that host's local LXD bridge. `velocity-proxy` needs a routable path to every world's Minecraft port (25565 inside each container) regardless of which host it lands on — cross-host reachability is the one piece of real infrastructure this design requires beyond a single box:

- **Single host (today):** trivial — everything's on `lxdbr0`, no extra work.
- **Multi-host:** put all hosts + `velocity-proxy` on a WireGuard mesh (or LXD OVN with a shared uplink network) so container IPs are mutually routable, or `lxc config device add` a `proxy` NIC device that publishes each world's port on the host's own address and give mgmt a stable `host-ip:published-port` per world instead of a container IP. Start with the WireGuard mesh — it composes cleanly with the trust model in §3 (same private network the LXD API traffic already lives on).

`velocity-proxy` polls `GET /api/v1/routes` on mgmt every few seconds:

```json
{
  "routes": [
    {"world": "world-overworld", "type": "overworld", "address": "10.0.1.21:25565", "default": true},
    {"world": "world-nether",    "type": "nether",    "address": "10.0.1.22:25565"},
    {"world": "world-lobby",     "type": "lobby",     "address": "10.0.2.10:25565"}
  ]
}
```

and reconciles its own `server-list`/forwarding config (via Velocity's plugin API) against it — no restart required to add or remove a world.

---

## 8. Snap Packaging Specifications

### A. `folia-smp-mgmt` snap

```yaml
name: folia-smp-mgmt
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

### B. `folia-smp-node` snap

```yaml
name: folia-smp-node
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

### C. `velocity-proxy` snap

Unchanged from the old plan's runtime (Velocity + Java), plus a routes-sync plugin dropped into `$SNAP_COMMON/proxy/plugins/` that polls mgmt's `/api/v1/routes` and calls Velocity's `ProxyServer.registerServer()` / `unregisterServer()` on diff. `plugs: [network, network-bind]`, unchanged confinement.

---

## 9. `folia-smp-node` Runtime Behavior

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

Inside the container, `folia-smp-node` reads those `user.folia.*` keys over the **devlxd socket** (`/dev/lxd/sock`, always present, no network config or credentials needed) — this is the same mechanism cloud-init uses inside LXD containers. On start it:

1. Reads its assignment (world name/type, jar URL, plugin manifest URL).
2. Downloads the jar + plugins into `$SNAP_COMMON/world` if not already staged (idempotent — restarts don't re-download).
3. Launches the JVM with region-scheduler-friendly flags (same `-XX:+UseZGC -XX:+ZGenerational` baseline as the old plan).
4. Serves a local `GET /healthz` and `GET /metrics` (TPS, tick times, memory, player count) on a loopback-bound port that mgmt scrapes over the container's network address.
5. On JVM exit, reports the crash reason locally (log tail) so mgmt's restart doesn't lose the "why."

No join token, no outbound registration call — the container's own config *is* its registration, and mgmt already knows it exists because mgmt is the one that ran `lxc launch`.

---

## 10. REST API Reference (`folia-smp-mgmt`)

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
| `GET`/`PUT` | `/api/v1/worlds/{name}/access` | Per-world whitelist toggle + ops list (§11) |
| `GET` | `/api/v1/routes` | Live routing table for `velocity-proxy` |
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

- **LuckPerms** (§14) as the permissions backend everywhere, backed by a shared MySQL/MariaDB instance so groups/tracks/permissions stay consistent across every world *and* the proxy — a player's rank follows them from `world-lobby` to `world-overworld` without per-world reconfiguration. **Implemented, with a correction from the original plan:** that MySQL instance is *not* scheduled as a `type: infra` world through mgmt — folia-smp-node only knows how to run a Folia/Paper JVM (§9), not arbitrary services, so a database server isn't something the current node agent can run. It's provisioned as its **own snap** instead, `folia-db` (`db/`) — bundles MariaDB itself and bootstraps a dedicated database/user with a generated password on first start, so getting the shared backend running is `snap install` + `snap start`, not a multi-step manual container setup. (`configs/luckperms/provision-mysql.sh`, a plain-LXD-container alternative, still exists for operators who'd rather not add another snap.) Either way, the operator points mgmt at whatever's listening via `luckperms_mysql_*` settings; what mgmt automates is every LuckPerms-enabled world's `config.yml` staying in sync with that instance on every reconcile pass (`folia_mgmt/luckperms.py`), not the database's deployment. `folia-db` is deliberately its own snap, not bundled into `folia-smp-mgmt`'s — worlds connect to it directly (LuckPerms plugin → MySQL wire protocol, never through mgmt's API), so a `snap refresh folia-smp-mgmt` should never interrupt every running world's active DB connection. One real limitation either way: LuckPerms reads its storage backend at plugin load time, so a world whose config just changed needs a restart to actually pick it up — pushing the file doesn't force one.
- Mgmt's dashboard doesn't reimplement LuckPerms' editor — it deep-links to LuckPerms' own web permissions editor (pointed at the shared MySQL backend) for group/track management, and only adds the two things that are genuinely cluster-level concerns:
  - a network-wide whitelist toggle,
  - a per-world ops list.
- `GET/PUT /api/v1/worlds/{name}/access` is the API surface for both, both implemented: mgmt resolves each `ops` name to a UUID via the Mojang API and pushes `ops.json` straight into the running container over LXD's file-push API (no exec round trip needed for a plain file write); `whitelist_enabled=true` pushes `whitelist.json` mirroring the *same* network-wide Discord-approved set §11C's access gate already uses, rather than maintaining a second, separate per-world guest list that could drift out of sync — the toggle means "also enforce network approval at this world's Paper level," not "give this world its own guest list." A periodic reconcile pass (§5) keeps it current as approvals change, not just at toggle time. One real gap remains: nothing here templates `server.properties`' own `white-list` flag or sends an RCON command, so actually turning Paper's enforcement of the pushed file on/off is still a follow-up.

### C. Requesting access, via Discord

"Can this person join at all" is a network-wide question, not a per-world one, so it's enforced once, at the front door. **Implemented:** `folia-routes-sync` (§8C) doubles as the access gate rather than being a separate plugin — it already polls mgmt on a timer for the routing table, so polling `GET /api/v1/access-requests/approved-uuids` on the same cycle and denying `LoginEvent` for anyone not in that set costs nothing extra to run. This means v1 doesn't need the shared MySQL `network_access` table at all — mgmt's own SQLite is the source of truth, same as everything else it tracks. The gate is opt-in (`FOLIA_ACCESS_GATE_ENABLED`, default off) so a fresh install never locks the operator out by surprise. Moving to a MySQL-backed `network_access` table (e.g. if something other than this proxy plugin ever needs to check approval) is a future migration, not a v1 requirement.

Getting approved is a Discord OAuth2 flow, not an operator manually running `whitelist add`:

1. Player hits mgmt's public "Request Access" page → **Sign in with Discord** (standard OAuth2 authorization-code flow; mgmt is a registered Discord application with `identify` + `guilds.members.read` scopes).
2. `GET /api/v1/auth/discord/callback` (mgmt) exchanges the code, then calls Discord's API *with the player's own token* to confirm they're a member of the configured guild — no bot needed for this check.
3. Player links a Minecraft username once (resolved to a UUID via the Mojang API) — stored alongside their Discord ID.
4. Policy, set per-cluster: `auto_approve_on_role: <role-id>` approves immediately if the player holds that Discord role; otherwise the request lands as `pending` for an operator to approve/deny from the Access panel (§12) or — for mods who live in Discord — via a bot command (§16).
5. Approval makes the player's UUID show up in `GET /api/v1/access-requests/approved-uuids` on the gate's next poll (§8C), and — once LuckPerms integration lands — would add them to its default group.

API surface:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/auth/discord/callback` | OAuth2 redirect target; creates/updates the access request |
| `POST` | `/api/v1/access-requests` | Create/upsert a request (operator token — used by folia-discord-bridge's `/request-access`, §16) |
| `GET` | `/api/v1/access-requests` | List requests, filterable by status (operator/admin) |
| `GET` | `/api/v1/access-requests/approved-uuids` | Polled by the proxy's access gate (viewer-role token) |
| `POST` | `/api/v1/access-requests/{id}/approve` | Approve → picked up by the gate's next poll |
| `POST` | `/api/v1/access-requests/{id}/deny` | Deny, with an optional reason shown to the player |

---

## 12. Web Dashboard

Same spirit as the old plan's single-page dashboard, extended with the concepts above:

- **Hosts view:** list of trusted LXD hosts, capacity bars, "Add host" flow (generates a join token for `folia-host-join`, §4).
- **Worlds view:** table of all worlds with type, host, TPS, players, phase; "Add world" (pick type/template, resource request, placement labels); per-world drain/snapshot/restore/migrate actions.
- **Access panel:** per-world whitelist/ops toggle (§11B), deep link to LuckPerms' web editor; operator user/role management (§11A, admin only).
- **Staging panel:** unchanged concept from the old plan (§13 below), now backed by LXD copy-from-snapshot instead of shell scripts.

---

## 13. Staging & Promotion Workflow

Staging a plugin change is now just LXD snapshot + copy, orchestrated by mgmt instead of a shell script talking to one hardcoded container name:

1. `POST /api/v1/worlds/world-overworld/snapshot` → `pre-plugin-<ts>`.
2. mgmt calls `lxc copy node-a:world-overworld/pre-plugin-<ts> node-a:world-overworld-staging -p folia-e-core` (declared as a `type: staging` world so it inherits `snapshot_policy: none` and is excluded from `velocity-proxy`'s route table by default).
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

Unaffected by the orchestration refactor above — still an asynchronous telemetry portal fed by proxy/world events, now simply pointed at whichever containers the scheduler happens to be running:

```
       ┌──────────────────┐       ┌──────────────────────────┐
       │   velocity-proxy  │       │   worlds (any host)       │
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

### Discord Bot (`folia-discord-bridge`, `bot/`)

**Implemented**, as a Python package (`discord.py` for the gateway/heartbeat/reconnect protocol — deliberately not hand-rolled; see the module docstring in `bot/src/folia_bot/bot.py` for why) with three slash commands, all backed by mgmt's REST API rather than any direct DB/LXD access:

- `/status` — embed of currently declared worlds and their phase/host, from `GET /api/v1/worlds`. Player counts and TPS aren't in that response (nothing in this codebase measures them yet), so the embed says so rather than fabricating numbers.
- `/request-access <minecraft_username>` — the in-Discord counterpart to §11C's web OAuth flow, via a new `POST /api/v1/access-requests` endpoint (operator-role token). Auto-approves locally from the inviting member's Discord roles (`FOLIA_BOT_AUTO_APPROVE_ROLE_ID`) without a round trip to Discord's API, since the bot already has that information from the interaction.
- `/leaderboard` — an explicit stub. Real leaderboards need the PostgreSQL/ClickHouse analytics store described in the portal section above, which hasn't been built; the command says so rather than being silently missing or showing fake numbers.

Join/quit/death announcements are `DiscordSRV`'s job (§14), not this bot's — that plugin relays directly from the proxy/worlds into a Discord channel independently.

Like the rest of this project's Discord/LXD-touching code, the bot itself has not been exercised against a live gateway connection or a registered Discord application — `discord.Client`/`CommandTree` construction and command registration were verified to build correctly against the real `discord.py` API, and everything in `embeds.py`/`access.py`/`mgmt_client.py` (the actual bot-specific logic) is unit-tested, but nobody has watched it come online in an actual Discord server.

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

1. Build `folia-smp-mgmt`, `folia-smp-node`, and `velocity-proxy` with `snapcraft --use-lxd`.
2. Publish to a private snap store channel or push `.snap` files directly to hosts.
3. Build (or point mgmt at) a base LXD image with `folia-smp-node` preinstalled, so container launch doesn't need a post-boot snap install step.

### Phase 3: Bring up the control plane

1. Install `folia-smp-mgmt` on its own container/VM (it needs almost no resources — 1 E-core class host is plenty).
2. Create the first operator account (`admin` role) and log in to the dashboard (§11A).
3. Issue a join token and run `folia-host-join` on the host from Phase 1 (§4):
   ```bash
   folia-smp-mgmt hosts create-join-token
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
   folia-smp-mgmt worlds create world-overworld --type overworld --cpu 6 --memory 12GB --labels cpu_type=p-core
   folia-smp-mgmt worlds create world-nether    --type nether    --cpu 2 --memory 3GB  --labels cpu_type=e-core
   folia-smp-mgmt worlds create world-lobby     --type lobby     --cpu 2 --memory 3GB  --labels cpu_type=e-core
   ```
2. Watch the scheduler place them; confirm `GET /api/v1/routes` and `velocity-proxy` pick them up automatically.
3. Pre-generate the overworld with `chunky start world 10000` via `lxc exec node-a:world-overworld -- ...` once it's running.

### Phase 5: Scale out

Adding a second host is Phase 1 steps 1–2 plus one `folia-host-join` run on the new machine. No topology diagram to redraw — the next `worlds create` (a minigame instance, a second nether shard, whatever) just has more capacity to land on.
