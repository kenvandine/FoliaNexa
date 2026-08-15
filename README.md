# FoliaSphere

A scheduler-driven cluster for running a multi-world [Folia](https://papermc.io/software/folia)/[Paper](https://papermc.io/software/paper) Minecraft SMP — one or more LXD hosts contribute capacity, and an orchestrator decides which worlds (survival, nether, lobby, minigames, ...) run where, moving them around as capacity changes. No fixed topology, no manually wired-together containers.

## Why

Running a serious multi-world Minecraft cluster by hand means hand-provisioning containers, hand-editing proxy configs every time a world moves, and hand-syncing plugin configs across every server. FoliaSphere turns that into: declare a world (name, type, size, plugins), and the cluster figures out placement, routing, permissions sync, and health recovery on its own.

## Architecture, at a glance

| Component | Snap | What it does |
| --- | --- | --- |
| [`mgmt/`](mgmt/) | `folia-smp-mgmt` | Orchestrator: REST API, scheduler, web dashboard, CLI. Trusts LXD hosts, places worlds on them, keeps them healthy. |
| [`node/`](node/) | `folia-smp-node` | Runs inside every world's container. Reads its assignment off the container's own LXD config, stages the jar/plugins, runs the JVM, reports health. |
| [`proxy/`](proxy/) | `velocity-proxy` (+ `folia-routes-sync` plugin) | The public entry point. Keeps its backend list in sync with mgmt's live routing table and optionally gates login against Discord-approved players — no restart to add or remove a world. |
| [`bot/`](bot/) | `folia-discord-bridge` | Discord bot: `/status`, `/request-access`, `/leaderboard`. |
| [`db/`](db/) | `folia-db` | Self-contained MariaDB instance backing LuckPerms, shared across every world. |

Full design rationale, data model, and API reference live in **[PLAN.md](PLAN.md)**. If you're going to read one doc before touching code, make it that one.

```
                              [ Public Internet ]
                                       │
                              velocity-proxy (routes synced live)
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
              LXD host: A        LXD host: B        LXD host: C
           world-overworld       world-lobby      world-minigame-*
           (folia-smp-node)    (folia-smp-node)   (folia-smp-node)
                    │                  │                  │
                    └──────────────────┼──────────────────┘
                                       ▼
                              folia-smp-mgmt (control plane)
                              ├─ schedules worlds onto hosts
                              ├─ keeps them healthy, restarts crashes
                              ├─ syncs LuckPerms config -> folia-db
                              └─ syncs Discord-approved access -> proxy
```

## Getting started

**Bootstrapping a real cluster from scratch?** See **[CLAUDE.md](CLAUDE.md)** — it's a phase-by-phase runbook (prerequisites → build/install the snaps → trust a host → declare worlds → bring up the proxy) with exact commands, not just a description.

**Want a running cluster fast?** [`configs/`](configs/) has a starter world set — one survival world with a full plugin loadout, two minigames (SkyWars, BedWars) — declared via one script once mgmt is up:

```bash
./configs/worlds/create-all.sh
```

**Just want to run the test suites?**

```bash
# mgmt (Python, 95 tests)
cd mgmt && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q

# node (Python, 15 tests)
cd node && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q

# bot (Python, 15 tests)
cd bot && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q

# proxy (Java, 24 tests — needs a JDK 21+)
cd proxy && ./gradlew test
```

## Status

149 automated tests passing across four components (95 mgmt, 15 node, 15 bot, 24 proxy). All five snaps build successfully with real `snapcraft` (that build pass caught and fixed two real bugs — see CLAUDE.md). Every mgmt API endpoint, the scheduler, the CLI, and the dashboard have been exercised against a real running server, not just a test client; `folia-db`'s entrypoint script was run end-to-end against real MariaDB binaries. Full breakdown of what's verified against real tooling versus what's written against a documented contract but not yet exercised live (installing/running the snaps, a registered Discord application) is in [CLAUDE.md](CLAUDE.md#whats-real-vs-whats-documented-but-unverified).

This is under active development — expect gaps, and check `PLAN.md`'s inline status notes before assuming a described feature is fully wired up.

## License

Not yet specified.
