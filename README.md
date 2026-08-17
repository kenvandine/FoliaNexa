# FoliaNexa

A scheduler-driven cluster for running a multi-world [Folia](https://papermc.io/software/folia)/[Paper](https://papermc.io/software/paper) Minecraft SMP — one or more LXD hosts contribute capacity, and an orchestrator decides which worlds (survival, nether, lobby, minigames, ...) run where, moving them around as capacity changes. No fixed topology, no manually wired-together containers.

## Why

Running a serious multi-world Minecraft cluster by hand means hand-provisioning containers, hand-editing proxy configs every time a world moves, and hand-syncing plugin configs across every server. FoliaNexa turns that into: declare a world (name, type, size, plugins), and the cluster figures out placement, routing, permissions sync, and health recovery on its own.

### Why LXD, not Kubernetes?

A deliberate choice, not a default. The workload shape doesn't fit Kubernetes' core value proposition: a world is one long-lived, sticky JVM, not a stateless replica you want horizontally scaled or freely rescheduled — the project already leans on things k8s doesn't give you natively:

- **Snapshots.** World backups and cross-host migration (PLAN.md §13) both ride on LXD's built-in instance snapshots and backup export/import. On k8s that's Velero or CSI VolumeSnapshots bolted on top, not something the platform hands you.
- **Per-host trust, not a single control plane.** Each LXD host is trusted individually via mTLS, scoped to a restricted, quota-capped project (PLAN.md §3) — a federation of independently-owned machines, not nodes joining one cluster's API server. That matches "any number of hosts you personally trust," not a shared/cloud cluster.
- **CPU pinning is a first-class feature.** Folia's regionized scheduler wants specific worlds pinned to specific physical P-/E-cores. LXD's `limits.cpu` does that directly; the k8s equivalent (CPU manager static policy + Guaranteed QoS) is more indirect to reason about.
- **No control-plane tax.** A k8s control plane (even k3s/microk8s) has real baseline resource overhead on every node — overhead that competes directly with the core budget this project wants dedicated to Folia's region-tick threads. LXD containers are close to native process overhead.

Where k8s would actually win: running on someone else's cloud cluster, or wanting the existing Helm/Prometheus/ingress ecosystem. Neither is this project's goal.

## Architecture, at a glance

| Component | Snap | What it does |
| --- | --- | --- |
| [`mgmt/`](mgmt/) | `folia-nexa-mgmt` | Orchestrator: REST API, scheduler, web dashboard, CLI. Trusts LXD hosts, places worlds on them, keeps them healthy, and serves a curated plugin catalog (repo-tracked, operator-extensible) worlds pick from. |
| [`node/`](node/) | `folia-nexa-node` | Runs inside every world's container. Reads its assignment off the container's own LXD config, stages the jar/plugins, runs the JVM, reports health. |
| [`proxy/`](proxy/) | `folia-nexa-proxy` (+ `folia-routes-sync` plugin) | The public entry point. Keeps its backend list in sync with mgmt's live routing table and optionally gates login against Discord-approved players — no restart to add or remove a world. |
| [`bot/`](bot/) | `folia-nexa-bot` | Discord bot: `/status`, `/request-access`, `/leaderboard`, plus a background loop that keeps access synced to live Discord role membership (grant/revoke, no operator action needed after the one-time `/request-access`). |
| [`db/`](db/) | `folia-nexa-db` | Self-contained MariaDB instance backing LuckPerms, shared across every world. |

Full design rationale, data model, and API reference live in **[PLAN.md](PLAN.md)**. If you're going to read one doc before touching code, make it that one.

```
                              [ Public Internet ]
                                       │
                              folia-nexa-proxy (routes synced live)
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
              LXD host: A        LXD host: B        LXD host: C
           world-overworld       world-lobby      world-minigame-*
           (folia-nexa-node)    (folia-nexa-node)   (folia-nexa-node)
                    │                  │                  │
                    └──────────────────┼──────────────────┘
                                       ▼
                              folia-nexa-mgmt (control plane)
                              ├─ schedules worlds onto hosts
                              ├─ keeps them healthy, restarts crashes
                              ├─ syncs LuckPerms config -> folia-nexa-db
                              └─ syncs Discord-approved access -> proxy
```

## Getting started

**Bootstrapping a real cluster from scratch?** See **[CLAUDE.md](CLAUDE.md)** — it's a phase-by-phase runbook (prerequisites → build/install the snaps → trust a host → declare worlds → bring up the proxy) with exact commands, not just a description.

**Designing and deploying a minigame world, or setting up the lobby players land in?** See **[docs/game-master-howto.md](docs/game-master-howto.md)** — sizing, picking plugins from the catalog, deploying, wiring it into the lobby's server selector, and tearing it down again.

**Want a running cluster fast?** [`configs/`](configs/) has a starter world set — one survival world with a full plugin loadout, two minigames (SkyWars, BedWars) — declared via one script once mgmt is up:

```bash
./configs/worlds/create-all.sh
```

**Picking plugins for a world?** [`mgmt/src/folia_mgmt/catalog.yaml`](mgmt/src/folia_mgmt/catalog.yaml) is the curated catalog — vetted external plugins pinned to known-good versions, plus room for your own in-house plugins (possibly from a separate repo; the catalog just needs a `download_url`). Browse it with `folia-nexa-mgmt plugins list`, the dashboard's "Plugins" tab, or the world-creation form's plugin picker; extend or override entries without a new mgmt release via a `plugin-catalog-override.yaml` in mgmt's state dir. See PLAN.md §14A.

**Building a new in-house plugin from scratch?** [`docs/plugin-dev/`](docs/plugin-dev/) is a three-part how-to series for exactly that — Ubuntu dev environment setup, Folia-safe plugin architecture (the region-scheduler APIs that replace the legacy Bukkit scheduler), and how to get a finished plugin into the catalog above. For actually writing one with Claude Code, the [`folia-plugin-scaffold`](.claude/skills/folia-plugin-scaffold/) skill automates this — scaffolds a real, buildable plugin project from a feature description, or from a Modrinth mod/plugin link (with an explicit no-code-copying, disclose-your-inspiration policy — it writes an original Folia-native implementation, not a code port).

**Just want to run the test suites?**

```bash
# mgmt (Python, 129 tests)
cd mgmt && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q

# node (Python, 15 tests)
cd node && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q

# bot (Python, 15 tests)
cd bot && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q

# proxy (Java, 24 tests — needs a JDK 21+)
cd proxy && ./gradlew test
```

## Status

183 automated tests passing across four components (129 mgmt, 15 node, 15 bot, 24 proxy). All five snaps build successfully with real `snapcraft` (that build pass caught and fixed two real bugs — see CLAUDE.md). Every mgmt API endpoint, the scheduler, the CLI, and the dashboard have been exercised against a real running server, not just a test client; `folia-nexa-db`'s entrypoint script was run end-to-end against real MariaDB binaries. Full breakdown of what's verified against real tooling versus what's written against a documented contract but not yet exercised live (installing/running the snaps, a registered Discord application) is in [CLAUDE.md](CLAUDE.md#whats-real-vs-whats-documented-but-unverified).

This is under active development — expect gaps, and check `PLAN.md`'s inline status notes before assuming a described feature is fully wired up.

## License

Not yet specified.
