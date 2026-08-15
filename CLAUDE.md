# folia-server

Multi-world Folia/Paper SMP cluster: `folia-smp-mgmt` (orchestrator),
`folia-smp-node` (in-container world agent), `velocity-proxy` (edge, with
the `folia-routes-sync` plugin), and `folia-discord-bridge` (Discord bot).
Full architecture and design rationale live in **`PLAN.md`** — read that
first for *why* things are shaped this way. This file is the *how*: how
to run the test suites, and how to bootstrap the whole stack on a fresh
host.

## Repo map

| Path | What | Language/tooling |
| --- | --- | --- |
| `mgmt/` | `folia-smp-mgmt` — FastAPI control plane, scheduler, dashboard, CLI | Python 3.12+, pytest |
| `node/` | `folia-smp-node` — in-container world runner/health agent | Python 3.12+, pytest |
| `proxy/` | `folia-routes-sync` — Velocity plugin (routing sync + access gate) | Java 21, Gradle |
| `bot/` | `folia-discord-bridge` — Discord bot (`/status`, `/request-access`, `/leaderboard`) | Python 3.12+, pytest |
| `db/` | `folia-db` — self-contained MariaDB snap for LuckPerms' shared backend | Bash, bundled MariaDB |
| `tools/folia-host-join.sh` | Automates trusting an LXD host into the cluster | Bash |
| `configs/worlds/*.sh` | Starter world declarations (CLI wrappers) | Bash |
| `configs/plugins/manifests/*.json` | Per-world plugin manifests `folia-smp-node` downloads from | JSON |
| `configs/luckperms/provision-mysql.sh` | Plain-LXD-container alternative to `db/` for operators who'd rather not add another snap | Bash |

Each of `mgmt/`, `node/`, `proxy/`, `bot/`, `db/` is an independent,
independently testable component with its own `snapcraft.yaml`. None of
the five `snapcraft.yaml` files have been build-tested with actual
`snapcraft` — they're written against its documented plugin behavior
(`python` plugin for mgmt/node/bot, `gradle`/`nil` for proxy, `nil` +
staged `mariadb-server` for db) but unverified end-to-end as an actual
snap build. `db/bin/run-folia-db.sh` itself, unlike the other four
snaps' entrypoints, *has* been run for real against the actual MariaDB
binaries (extracted from the `.deb`s, outside snap confinement) — see
its own comments for what that did and didn't prove.

## Running the test suites

```bash
# mgmt (Python) — 95 tests
cd mgmt && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

# node (Python) — 15 tests
cd node && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

# bot (Python) — 15 tests, no live Discord connection needed
cd bot && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

# proxy (Java) — 24 tests, real Gradle + real velocity-api jar
cd proxy && ./gradlew test   # needs a JDK 21+ on PATH, or JAVA_HOME set
```

If there's no system JDK available (this project was developed in a
sandbox with none, and no root to install one), a portable one works
fine — Gradle doesn't care where `JAVA_HOME` points:

```bash
curl -sL -o /tmp/jdk.tar.gz \
  "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse"
mkdir -p ~/.local/jdk21 && tar -xzf /tmp/jdk.tar.gz -C ~/.local/jdk21 --strip-components=1
cd proxy && JAVA_HOME=~/.local/jdk21 ./gradlew test
```

All three suites are independent — no shared fixtures, no ordering
requirement. `mgmt`'s and `node`'s tests never touch a real LXD host or
Mojang/Discord API (everything's faked/mocked); `proxy`'s tests compile
and run against the *real* `velocity-api` jar but don't start a real
Velocity server.

## Bootstrapping a fresh host from scratch

This is the production path — building and installing the actual snaps.
Two things below are genuinely unverified end-to-end (flagged inline):
the snap builds themselves, and anything touching a live LXD daemon or a
live Discord application. Everything else (the CLI flow, the API
contracts, the plugin manifest shape) has been exercised against a real
running server in development.

### Phase 0 — prerequisites

- Ubuntu 24.04+ (or anything `core24` snaps target) on the mgmt host and
  every host that will run worlds.
- `snapd` and LXD installed on every host that will run worlds:
  ```bash
  sudo snap install lxd
  sudo lxd init   # pick a storage backend — this script won't choose for you
  ```
- `snapcraft` on whichever machine builds the snaps (doesn't need to be a
  cluster member):
  ```bash
  sudo snap install snapcraft --classic
  ```

### Phase 1 — build and install `folia-smp-mgmt`

```bash
cd mgmt && snapcraft   # unverified — see the repo map note above
sudo snap install ./folia-smp-mgmt_0.1_amd64.snap --dangerous
sudo snap start folia-smp-mgmt.daemon
```

No published snap store listing exists yet, hence `--dangerous` (installs
an unsigned local snap).

### Phase 2 — bootstrap the first admin account and log in

```bash
sudo folia-smp-mgmt bootstrap-admin admin <a-real-password>
folia-smp-mgmt login https://<mgmt-host>:8443 admin <a-real-password>
```

`bootstrap-admin` talks to the local DB directly (no HTTP), so it only
works run *on* the mgmt host itself — see PLAN.md §11A.

### Phase 3 — trust the first LXD host

On the host that will run worlds (can be the same machine as mgmt, or a
separate one — PLAN.md §3):

```bash
folia-smp-mgmt hosts create-join-token   # run from wherever you're logged in
sudo ./tools/folia-host-join.sh \
  --mgmt-url https://<mgmt-host>:8443 \
  --join-token <token-from-above> \
  --address <this-host-ip>
```

The script's LXD-side steps (enabling the remote API, creating the
`folia` project with quotas, generating a trust token) are real and
independently useful even before mgmt exists — use `--skip-enroll` to
stop there. The final enrollment call needs `folia-smp-mgmt`'s API
running and reachable.

Confirm it worked:

```bash
folia-smp-mgmt hosts list
```

### Phase 4 — build and install `folia-smp-node`'s base image

Worlds are LXD containers launched from an image with `folia-smp-node`
preinstalled (PLAN.md §17 Phase 2 step 3). Build the snap, then bake it
into a base image the scheduler's `launch_container` calls reference by
alias (`folia-node-base` — `mgmt/src/folia_mgmt/scheduler.py`):

```bash
cd node && snapcraft   # unverified, same caveat as mgmt's build
# then, on an LXD host: launch a container, `snap install
# ./folia-smp-node_0.1_amd64.snap --dangerous`, and publish it as an
# image aliased folia-node-base — see `lxc publish` / `lxc image alias`.
```

### Phase 5 — declare worlds

Once a host is trusted, `worlds create` will actually place things
instead of leaving them `pending`. Hand-rolled:

```bash
folia-smp-mgmt worlds create world-overworld --type overworld --cpu 6 --memory 12GB --labels cpu_type=p-core
```

Or use the starter set in `configs/worlds/` — one survival world, two
minigames, each with a real plugin loadout declared via `--plugin` (see
`configs/worlds/create-all.sh` and the manifests in
`configs/plugins/manifests/`). **Before running these**, replace the
placeholder plugin URLs in those manifest JSON files with real download
links and host them somewhere `folia-smp-mgmt`'s `artifacts_base_url`
setting points at (default `https://artifacts.internal`, overridable via
`FOLIA_MGMT_ARTIFACTS_BASE_URL`) — `folia-smp-node` won't start a world
until it can actually fetch its jar/plugins from there. The minigame
plugins listed (SkyWarsReloaded, BedWars1058) are common, well-known
choices, not something verified Folia-compatible in this repo — check
before relying on them; classic Bukkit-era minigame plugins often assume
single-threaded world ticking that Folia's regionized scheduler doesn't
guarantee.

```bash
./configs/worlds/create-all.sh
```

### Phase 6 — shared MySQL for LuckPerms (optional)

Only needed if any declared world includes `LuckPerms` in its plugin
list (the survival starter config in `configs/worlds/` does). Not
through mgmt's scheduler either way — folia-smp-node only runs
Folia/Paper JVMs, not a database server (see
`mgmt/src/folia_mgmt/luckperms.py`'s module docstring). Two options:

**`folia-db`** (recommended — self-contained, bundles MariaDB itself):

```bash
cd db && snapcraft   # unverified as an actual snap build, see repo map note above
sudo snap install ./folia-db_11.8_amd64.snap --dangerous
sudo snap start folia-db.daemon
sudo snap run folia-db.show-credentials   # prints the FOLIA_MGMT_LUCKPERMS_MYSQL_* vars
```

First start bootstraps a dedicated database/user with a generated
password automatically — nothing else to run.

**`configs/luckperms/provision-mysql.sh`** (alternative, if you'd rather
not add another snap — a plain LXD container with MariaDB installed via
apt):

```bash
./configs/luckperms/provision-mysql.sh
```

Either way, set the printed `FOLIA_MGMT_LUCKPERMS_MYSQL_*` variables on
the mgmt host. Once set, every running world with `LuckPerms` in its
plugin list gets its `config.yml` kept in sync automatically — existing
worlds pick it up on their next restart (LuckPerms reads its storage
backend at plugin load time, not live).

### Phase 7 — build and install `velocity-proxy`

```bash
cd proxy && snapcraft   # unverified, same caveat as the others
sudo snap install ./velocity-proxy_3.5.1_amd64.snap --dangerous
```

It needs `FOLIA_MGMT_URL` and `FOLIA_MGMT_API_TOKEN` (an mgmt API token —
viewer role is enough) in `$SNAP_COMMON/proxy/config.env` before
starting; see `FoliaRoutesSyncPlugin`'s javadoc for the full env var
list, including the opt-in `FOLIA_ACCESS_GATE_ENABLED`.

There's no CLI command for user/token management yet (only `hosts` and
`worlds` have one — see `mgmt/src/folia_mgmt/cli.py`), so create the
proxy's service account directly against the API:

```bash
TOKEN=$(cat ~/.config/folia-smp-mgmt/cli.json | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -X POST https://<mgmt-host>:8443/api/v1/users \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username": "velocity-proxy", "password": "<a-real-password>", "role": "viewer"}'
curl -X POST https://<mgmt-host>:8443/api/v1/users/velocity-proxy/api-token \
  -H "Authorization: Bearer $TOKEN"
# use the returned token as FOLIA_MGMT_API_TOKEN below
```

```bash
sudo snap start velocity-proxy.daemon
```

### Phase 8 — build and install `folia-discord-bridge` (optional)

```bash
cd bot && snapcraft   # unverified, same caveat as the others
sudo snap install ./folia-discord-bridge_0.1_amd64.snap --dangerous
```

Needs `DISCORD_BOT_TOKEN`, `FOLIA_MGMT_URL`, and `FOLIA_MGMT_API_TOKEN`
(operator role — it creates access requests on other users' behalf, more
than a read-only action) as environment. `DISCORD_GUILD_ID` and
`FOLIA_BOT_AUTO_APPROVE_ROLE_ID` are optional — see the module docstring
in `bot/src/folia_bot/bot.py`. Requires a registered Discord application
with a bot user and the `applications.commands` scope invited to your
server, none of which this repo can set up for you.

```bash
sudo snap start folia-discord-bridge.daemon
```

## What's real vs. what's documented-but-unverified

Verified with real tooling in this repo's development (real HTTP
servers, real Gradle+Velocity API compilation, real running mgmt server
hit with curl/the CLI, real discord.py client/command-tree construction):

- Every mgmt API endpoint, the scheduler's placement/health-check/
  recovery/migration logic, the CLI, the dashboard.
- `folia-routes-sync`'s routing diff, JSON parsing, and access-gate logic
  — compiled and unit-tested against the real `velocity-api` jar.
- `folia-discord-bridge`'s embed-building, auto-approve decision logic,
  and mgmt API client — unit-tested; `discord.Client`/`CommandTree`
  construction and command registration verified to build correctly
  against the real `discord.py` API.
- `folia-host-join.sh`'s LXD-prep steps (project creation, trust token
  generation) — bash-syntax-checked and logically reviewed, not run
  against a live LXD daemon in this environment.
- `luckperms.py`'s config.yml rendering and the reconcile-loop sync logic
  that decides which worlds get it pushed.
- `db/bin/run-folia-db.sh` — actually run end-to-end (`mariadb-install-db`
  init, `mariadbd` startup, database/user bootstrap, a real client
  connection with the generated credentials, confirming root has no
  wildcard/remote grant) against the real MariaDB binaries extracted from
  Ubuntu's `.deb` packages. This caught two real path bugs (`mariadb-
  install-db` living under `usr/bin/`, not `usr/sbin/`; needing an
  explicit `--basedir` under a non-`/usr` prefix like a snap's `$SNAP`)
  before they'd have surfaced as a broken first boot. Not run inside
  actual snap confinement, though — see below.

Written against documented API contracts but **not** exercised against
live infrastructure:

- Every `LXDClient` method (mTLS bootstrap, instance CRUD, file push,
  backup export/import for migration) — no LXD daemon was available to
  test against.
- The Discord OAuth2 flow, Mojang UUID resolution, and the bot's actual
  gateway connection — no registered Discord application was available
  to test against.
- `configs/luckperms/provision-mysql.sh` and the LuckPerms config.yml
  format itself (verify plugin config keys against whatever LuckPerms
  version you actually deploy — they can drift between major versions).
- All five `snapcraft.yaml` files as actual snap builds — no `snapcraft`
  binary was available. `db/`'s specifically: strict confinement could
  plausibly restrict something `mariadbd` wants (raw sockets, certain
  filesystem operations) that running the binary directly, unconfined,
  wouldn't have caught — confinement-related failures are the main risk
  left unverified for this one.

If you're picking up this project to actually run it: those are the
places to validate first, roughly in that order.
