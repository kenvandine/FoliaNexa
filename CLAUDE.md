# FoliaNexa

Multi-world Folia/Paper SMP cluster: `folia-nexa-mgmt` (orchestrator),
`folia-nexa-node` (in-container world agent), `folia-nexa-proxy` (edge, with
the `folia-routes-sync` plugin), and `folia-nexa-bot` (Discord bot).
Full architecture and design rationale live in **`PLAN.md`** — read that
first for *why* things are shaped this way. This file is the *how*: how
to run the test suites, and how to bootstrap the whole stack on a fresh
host.

## Repo map

| Path | What | Language/tooling |
| --- | --- | --- |
| `mgmt/` | `folia-nexa-mgmt` — FastAPI control plane, scheduler, dashboard, CLI | Python 3.12+, pytest |
| `node/` | `folia-nexa-node` — in-container world runner/health agent | Python 3.12+, pytest |
| `proxy/` | `folia-routes-sync` — Velocity plugin (routing sync + access gate) | Java 21, Gradle |
| `bot/` | `folia-nexa-bot` — Discord bot (`/status`, `/request-access`, `/leaderboard`) | Python 3.12+, pytest |
| `db/` | `folia-nexa-db` — self-contained MariaDB snap for LuckPerms' shared backend | Bash, bundled MariaDB |
| `tools/folia-host-join.sh` | Automates trusting an LXD host into the cluster | Bash |
| `tools/folia-nexa-spawn.sh` | Local dev tool: spins up a single-machine Folia server with a plugin built from local source loaded, for fast plugin iteration — no cluster/mgmt/proxy involved. See `docs/plugin-dev/01-environment-setup.md` §1.8 | Bash |
| `configs/worlds/*.sh` | Starter world declarations (CLI wrappers) | Bash |
| `mgmt/src/folia_mgmt/catalog.yaml` | Curated plugin catalog (PLAN.md §14A) — mgmt generates per-world manifests from this + a world's `plugins` list, no hand-authored manifest files | YAML |
| `mgmt/src/folia_mgmt/datapacks.yaml` | Curated data pack catalog (e.g. Matcha) — same pattern as `catalog.yaml` but for vanilla data packs, staged into `<level-name>/datapacks/` instead of `plugins/`; see `datapack_catalog.py` | YAML |
| `configs/luckperms/provision-mysql.sh` | Plain-LXD-container alternative to `db/` for operators who'd rather not add another snap | Bash |
| `docs/game-master-howto.md` | Task-oriented guide: designing/deploying a minigame world and configuring the lobby (PLAN.md §14B) | Markdown |
| `docs/plugin-dev/` | Three-part how-to series: dev environment setup, Folia-safe plugin architecture, submitting a plugin for catalog review | Markdown |
| `.claude/skills/folia-plugin-scaffold/` | Claude Code skill that operationalizes `docs/plugin-dev/` — scaffolds and writes a real Folia/Paper plugin, from a description or a Modrinth mod/plugin link | Markdown + templates |
| `.claude/skills/cluster-onboarding/` | Claude Code skill that operationalizes CLAUDE.md's bootstrap phases + `docs/vps-edge-deployment.md` into an interactive runbook for standing up the VPS edge, DNS, and trusting LXD hosts | Markdown |
| `portal/` | `folia-nexa-portal` — public player hub (leaderboards, profiles, playtime heatmaps, who's online) — static HTML/CSS/JS (no build step) served by a small stdlib Python daemon, deployed to the VPS edge as its own snap like every other component (PLAN.md §7A) | HTML/CSS/vanilla JS + Python 3.12+ (server), pytest |
| `deploy/vps/` | WireGuard tunnel + Caddy config for the VPS edge (PLAN.md §7A) — see `docs/vps-edge-deployment.md` | Bash, Caddyfile, WireGuard config templates |

In-house plugin source doesn't live in this repo — it's in the sibling
[`folianexa-plugins`](https://github.com/kenvandine/folianexa-plugins)
repo (one top-level directory per plugin, e.g. `campus-lobby/` for the
`CampusLobby` lobby-scene plugin), per `docs/plugin-dev/03-submitting-
for-review.md`. `catalog.yaml`'s `download_url` points at a release
built from there; this repo only ever needs that URL, not the source.

Each of `mgmt/`, `node/`, `proxy/`, `bot/`, `db/`, `portal/` is an
independent, independently testable component with its own
`snapcraft.yaml`. **All six build successfully with real `snapcraft`
9.0.1** (`snapcraft` from each directory) — confirmed in this
environment against a real LXD-based build backend, not just reviewed.
That build pass caught two real bugs that a review wouldn't have:
`proxy/snapcraft.yaml` was pointed at
`api.papermc.io/v2`, which PaperMC has since sunset in favor of
`fill.papermc.io/v3` (different host, different response shape); and
`bot/snapcraft.yaml`'s `summary` field exceeded snap metadata's 78-
character limit. Both fixed. (`portal/`'s build was verified further
than a bare pass/fail — the resulting `.snap` was unsquashed and its
contents listed, confirming `snapcraft.yaml`'s `override-build` actually
copied the static HTML/CSS/JS into the snap alongside the Python venv,
not just that `snapcraft` exited 0.) (`proxy/snapcraft.yaml`'s later Bedrock/
GeyserMC addition — the `geyser-plugins` part, PLAN.md §7B — was not
re-verified against a real `snapcraft` build *in this AI dev-sandbox
environment*; no `snapcraft`/`snapd` was available in the environment
that change was made in. Its two download URLs were fetched and
sha256-checked directly instead — see that part's comments. It, and the
later Velocity 3.5.1 -> 4.0.0 bump alongside it, **have** since been
confirmed for real by the operator, outside this sandbox: a real
`snapcraft` build, `snap install --dangerous`, and start on the live
`play.sullivan.linuxgroove.com` proxy — see the Bedrock client support
entry in "What's real vs. what's documented-but-unverified" below for
what that run caught and what's still open.) `db/bin/run-folia-nexa-db.sh`
was additionally run end-to-end against the real MariaDB binaries
(outside snap confinement, extracted from the `.deb`s directly) — see
its own comments for what that did and didn't prove. None of `mgmt/`,
`node/`, `bot/`, `db/`, or `portal/` have been **installed** (`snap
install ... --dangerous`) or actually started in this AI dev-sandbox
environment — no root/interactive-sudo access was available for that —
so their runtime behavior under real strict confinement is still
unverified; only the build step is confirmed for those five. `proxy/`
is the exception, per the above.

## Running the test suites

```bash
# mgmt (Python) — 314 tests
cd mgmt && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

# node (Python) — 17 tests
cd node && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

# bot (Python) — 15 tests, no live Discord connection needed
cd bot && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

# proxy (Java) — 39 tests, real Gradle + real velocity-api jar
cd proxy && ./gradlew test   # needs a JDK 21 on JAVA_HOME, plus a JDK 25 installed

# portal (Python) — 4 tests, real HTTP against the actual static file server
cd portal && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

`proxy/build.gradle.kts` compiles against `velocity-api:4.0.0` (bumped
from `3.5.1`, see `proxy/snapcraft.yaml`'s `geyser-plugins` part for
why), whose own classfiles require a Java 25 toolchain. Gradle 8.10
(this project's wrapper) can't run its own daemon on a JDK 25 launcher
— confirmed locally, `./gradlew` under `JAVA_HOME` pointed at a JDK 25
fails immediately with a bare, stack-trace-less `25.0.3` exception
before it even reaches toolchain provisioning. So `JAVA_HOME` needs to
point at a JDK **21** (for Gradle's own launcher) while a JDK **25** is
also present on the machine for Gradle's toolchain auto-detection (it
scans `/usr/lib/jvm` and similar by default, no extra config needed) to
pick up for the actual compile. If there's no system JDK available at
all (this project was developed in a sandbox with none, and no root to
install one), portable ones work fine — Gradle doesn't care where
`JAVA_HOME` points, just that a 21 and a 25 both exist on disk:

```bash
curl -sL -o /tmp/jdk21.tar.gz \
  "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse"
mkdir -p ~/.local/jdk21 && tar -xzf /tmp/jdk21.tar.gz -C ~/.local/jdk21 --strip-components=1
curl -sL -o /tmp/jdk25.tar.gz \
  "https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jdk/hotspot/normal/eclipse"
mkdir -p ~/.local/jdk25 && tar -xzf /tmp/jdk25.tar.gz -C ~/.local/jdk25 --strip-components=1
cd proxy && JAVA_HOME=~/.local/jdk21 ./gradlew test
```

All five suites are independent — no shared fixtures, no ordering
requirement. `mgmt`'s and `node`'s tests never touch a real LXD host or
Mojang/Discord API (everything's faked/mocked); `proxy`'s tests compile
and run against the *real* `velocity-api` jar but don't start a real
Velocity server; `portal`'s tests start the real `serve.py` HTTP server
on a real (ephemeral) socket rather than mocking it.

## Bootstrapping a fresh host from scratch

This is the production path — building and installing the actual snaps.
The builds themselves are confirmed (see the repo map note above); what's
still genuinely unverified end-to-end is *installing* them (needs root,
unavailable in this environment) and anything touching a live LXD daemon
or a live Discord application. Everything else (the CLI flow, the API
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

### Phase 1 — build and install `folia-nexa-mgmt`

```bash
cd mgmt && snapcraft   # confirmed to build — see the repo map note above
sudo snap install ./folia-nexa-mgmt_0.1_amd64.snap --dangerous
sudo snap start folia-nexa-mgmt.daemon
```

No published snap store listing exists yet, hence `--dangerous` (installs
an unsigned local snap).

### Phase 2 — bootstrap the first admin account and log in

```bash
sudo folia-nexa-mgmt bootstrap-admin admin <a-real-password>
folia-nexa-mgmt login https://<mgmt-host>:8443 admin <a-real-password>
```

`bootstrap-admin` talks to the local DB directly (no HTTP), so it only
works run *on* the mgmt host itself — see PLAN.md §11A.

### Phase 3 — trust the first LXD host

On the host that will run worlds (can be the same machine as mgmt, or a
separate one — PLAN.md §3):

```bash
folia-nexa-mgmt hosts create-join-token   # run from wherever you're logged in
sudo ./tools/folia-host-join.sh \
  --mgmt-url https://<mgmt-host>:8443 \
  --join-token <token-from-above> \
  --address <this-host-ip>
```

The script's LXD-side steps (enabling the remote API, creating the
`folia` project with quotas, generating a trust token) are real and
independently useful even before mgmt exists — use `--skip-enroll` to
stop there. The final enrollment call needs `folia-nexa-mgmt`'s API
running and reachable.

Confirm it worked:

```bash
folia-nexa-mgmt hosts list
```

### Phase 4 — build and install `folia-nexa-node`'s base image

Worlds are LXD containers launched from an image with `folia-nexa-node`
preinstalled (PLAN.md §17 Phase 2 step 3). Build the snap, then bake it
into a base image the scheduler's `launch_container` calls reference by
alias (`folia-node-base` — `mgmt/src/folia_mgmt/scheduler.py`):

```bash
cd node && snapcraft   # confirmed to build, same as mgmt
# then, on an LXD host: launch a container, `snap install
# ./folia-nexa-node_0.1_amd64.snap --dangerous`, and publish it as an
# image aliased folia-node-base — see `lxc publish` / `lxc image alias`.
```

### Phase 5 — declare worlds

Once a host is trusted, `worlds create` will actually place things
instead of leaving them `pending`. Hand-rolled:

```bash
folia-nexa-mgmt worlds create world-overworld --type overworld --cpu 6 --memory 12GB --labels cpu_type=p-core
```

Or use the starter set in `configs/worlds/` — one survival world, two
minigames, each with a real plugin loadout declared via `--plugin` (ids
from `mgmt/src/folia_mgmt/catalog.yaml`, browsable with `folia-nexa-mgmt
plugins list`; see `configs/worlds/create-all.sh`). **Before running
these**, note two separate things need to be reachable:

- The engine jar comes from `folia-nexa-mgmt`'s `artifacts_base_url`
  setting (default `https://artifacts.internal`, overridable via
  `FOLIA_MGMT_ARTIFACTS_BASE_URL`) — `folia-nexa-node` fetches
  `{artifacts_base_url}/{engine}/{version}/{engine}.jar` from there.
- Declaring any `--plugin` requires `FOLIA_MGMT_PUBLIC_URL` to be set to
  this mgmt instance's own reachable address — worlds fetch their plugin
  manifest from `{public_url}/api/v1/worlds/{name}/plugins-manifest`,
  which mgmt generates live from the catalog (PLAN.md §14A). For the snap,
  set it with `sudo snap set folia-nexa-mgmt public-url=https://<mgmt-
  host>:8443` (mirrors `listen-port` below — `run-mgmt-daemon.sh` re-reads
  it via snapctl on every daemon start, and `snap/hooks/configure`
  restarts the daemon when it changes). Several
  catalog entries (e.g. `HuskClaims`, `ItemsAdder`) still have a
  placeholder `download_url: null` — populate those (in
  `catalog.yaml` or a `plugin-catalog-override.yaml` in mgmt's state
  dir) with real download links before relying on them; a plugin with no
  `download_url` is silently skipped in the generated manifest, not an
  error.

Vanilla data packs (e.g. the Matcha gameplay-tweaks pack) work the same
way, one catalog over: `--datapack <id>` (ids from
`mgmt/src/folia_mgmt/datapacks.yaml`, browsable with `folia-nexa-mgmt
datapacks list`), needs `FOLIA_MGMT_PUBLIC_URL` for the same reason
(`{public_url}/api/v1/worlds/{name}/datapacks-manifest`), and an
operator override file (`datapack-catalog-override.yaml` in mgmt's state
dir) works the same way. The one real difference: `folia-nexa-node`
stages a world's data packs into `<level-name>/datapacks/` inside the
world save (assumed level-name `world`, the vanilla default — this
codebase never templates `server.properties`, so a world running a
non-default level-name won't get its data packs staged correctly; see
`node/src/folia_node/staging.py`'s `LEVEL_NAME` comment) rather than a
server-root `plugins/` folder, and they take effect at the server's next
first-boot/world-generation rather than needing a JVM plugin loader at
all.

The minigame plugins listed (SkyWarsReloaded, BedWars1058) are common,
well-known choices, not something verified Folia-compatible in this
repo — check before relying on them; classic Bukkit-era minigame plugins
often assume single-threaded world ticking that Folia's regionized
scheduler doesn't guarantee.

```bash
./configs/worlds/create-all.sh
```

### Phase 6 — shared MySQL for LuckPerms (optional)

Only needed if any declared world includes `LuckPerms` in its plugin
list (the survival starter config in `configs/worlds/` does). Not
through mgmt's scheduler either way — folia-nexa-node only runs
Folia/Paper JVMs, not a database server (see
`mgmt/src/folia_mgmt/luckperms.py`'s module docstring). Two options:

**`folia-nexa-db`** (recommended — self-contained, bundles MariaDB itself):

```bash
cd db && snapcraft   # confirmed to build — see the repo map note above
sudo snap install ./folia-db_11.8_amd64.snap --dangerous
sudo snap start folia-nexa-db.daemon
sudo snap run folia-nexa-db.show-credentials   # prints the FOLIA_MGMT_LUCKPERMS_MYSQL_* vars
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

### Phase 7 — build and install `folia-nexa-proxy`

```bash
cd proxy && snapcraft   # confirmed to build, same as the others (see note below on the Bedrock addition)
sudo snap install ./folia-nexa-proxy_4.0.0_amd64.snap --dangerous
```

It needs `FOLIA_MGMT_URL` and `FOLIA_MGMT_API_TOKEN` (an mgmt API token —
viewer role is enough) in `$SNAP_COMMON/proxy/config.env` before
starting; see `FoliaRoutesSyncPlugin`'s javadoc for the full env var
list, including the opt-in `FOLIA_ACCESS_GATE_ENABLED`.

The snap now also bundles Geyser-Velocity + floodgate-velocity, so
Bedrock (console/mobile/Win10) clients can join the same proxy on
`:19132/udp` with zero extra config beyond opening that port (see
"Bootstrapping" note in PLAN.md §7B, and Phase 9 below for the VPS-edge
firewall rule). The Velocity 3.5.1 -> 4.0.0 / JDK 21 -> 25 bump (PLAN.md
§7B's Bedrock-support section, in "What's real vs. what's documented-
but-unverified" below, has the full incompatibility history) **has**
now been through a real `snapcraft` build + `snap install --dangerous` +
start on the live `play.sullivan.linuxgroove.com` proxy — which is what
caught a real `run-velocity.sh` bug the dev-environment verification
alone didn't: `velocity.jar` used to be seeded into `$SNAP_COMMON` once
and never refreshed, so the upgrade silently kept the daemon running the
*old* 3.5.1 engine jar against the *new* Geyser 2.11.1 plugin, and
crashed with the exact incompatibility this bump was meant to fix — now
fixed by force-refreshing `velocity.jar` on every start, same as
`folia-routes-sync-*.jar` already was. See that section below for what's
confirmed vs. still open (mainly: a real Bedrock client actually
reconnecting once this fixed script has been exercised by a real
upgrade).

There's no CLI command for user/token management yet (only `hosts` and
`worlds` have one — see `mgmt/src/folia_mgmt/cli.py`), so create the
proxy's service account directly against the API:

```bash
TOKEN=$(cat ~/.config/folia-nexa-mgmt/cli.json | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -X POST https://<mgmt-host>:8443/api/v1/users \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"username": "folia-nexa-proxy", "password": "<a-real-password>", "role": "viewer"}'
curl -X POST https://<mgmt-host>:8443/api/v1/users/folia-nexa-proxy/api-token \
  -H "Authorization: Bearer $TOKEN"
# use the returned token as FOLIA_MGMT_API_TOKEN below
```

```bash
sudo snap start folia-nexa-proxy.daemon
```

#### Connecting a Minecraft client

`folia-nexa-proxy` is the single public entry point for both client
families — players never connect to a world container directly, and
never pick a world by address; the proxy's `default`-flagged lobby route
(PLAN.md §14B) is what they land on first.

| Client | Add-server address | Port | Protocol |
| --- | --- | --- | --- |
| Java Edition | proxy host's IP or hostname | **25565** | TCP |
| Bedrock (console/mobile/Win10) | proxy host's IP or hostname | **19132** | UDP (RakNet) |

- **Java**: "Add Server" → the proxy host's address, port `25565` (Velocity's
  default `bind` in `proxy/snapcraft.yaml`; omit the port if it's the
  default 25565, most Java clients assume it).
- **Bedrock**: "Add Server" → the same proxy host's address, but port
  `19132` and UDP — this only works if the snap was built with the
  bundled Geyser-Velocity + floodgate-velocity part (on by default, see
  above) and that port is reachable (open it in any firewall in front of
  the proxy host, e.g. `sudo ufw allow 19132/udp`). No separate DNS
  record is needed for Bedrock — unlike Java, it has no SRV-record
  convention, so players type the host and `19132` into two separate
  fields in their client.

Both protocols terminate at the same proxy process and route through the
same live backend list (PLAN.md §7B) — a Bedrock player reaches the exact
same worlds a Java player does, translated transparently by Geyser. If
this cluster is deployed behind the VPS edge (Phase 9 below) instead of
directly, see `docs/vps-edge-deployment.md`'s own connecting section —
the addresses differ (VPS public IP instead of the home host) but the
ports are identical.

### Phase 8 — build and install `folia-nexa-bot` (optional)

```bash
cd bot && snapcraft   # confirmed to build, same as the others
sudo snap install ./folia-nexa-bot_0.1_amd64.snap --dangerous
```

Needs `DISCORD_BOT_TOKEN`, `FOLIA_MGMT_URL`, and `FOLIA_MGMT_API_TOKEN`
(operator role — it creates access requests on other users' behalf, more
than a read-only action) as environment. `DISCORD_GUILD_ID` and
`FOLIA_BOT_AUTO_APPROVE_ROLE_ID` are optional — see the module docstring
in `bot/src/folia_bot/bot.py`. Requires a registered Discord application
with a bot user and the `applications.commands` scope invited to your
server, none of which this repo can set up for you.

**Before starting this snap**, enable the privileged **Server Members
Intent** for that application in the Discord Developer Portal (Bot page
→ Privileged Gateway Intents) — the bot's Discord role-sync feature
(PLAN.md §11C) requests the `members` gateway intent unconditionally at
connect time, and discord.py refuses to log in at all
(`PrivilegedIntentsRequired`) if the portal side hasn't been flipped on
first, even if you never configure a gate role. This is a one-time
per-application setting, done once in the portal, not per-deploy.

```bash
sudo snap start folia-nexa-bot.daemon
```

The bot commands (`/status`, `/request-access`, `/leaderboard`) and the
Discord role-sync loop are documented in full in the module docstring at
the top of `bot/src/folia_bot/bot.py` — read that first for exact
behavior, including what each optional env var does when unset. Short
version of the parts most likely to surprise an operator:

- Auto-approving someone by Discord role and *revoking* them
  automatically if that role is later removed both go through the
  dashboard's new "Discord role gate" card (Access Requests tab in
  `folia-nexa-mgmt`'s web UI) — an enable toggle plus the guild ID and
  role ID to gate on. This is **not** an env var on this snap; it's a
  cluster-wide setting stored in mgmt's own DB and polled by the bot
  every 60s, so a change in the dashboard takes effect without
  restarting either the bot or mgmt.
- Even with the role gate enabled and a player already holding the
  configured role, they still need to run `/request-access
  <minecraft_username>` (or the web OAuth flow) **once** — that's the
  only way mgmt learns which Minecraft account belongs to that Discord
  user. Role-sync only manages players it already knows about; it never
  invents a new access request for a Discord member it's never heard
  from, regardless of their roles. Granting someone the Discord role by
  itself does not get them onto the whitelist.
- After that one-time link, ongoing access tracks the role live —
  gaining or losing it in Discord grants/revokes access automatically
  (near-instantly via a gateway member-update event, with a 15-minute
  periodic re-sync as a safety net for anything missed), no repeat
  command or operator action needed either way. Losing the role blocks
  future joins; it does not disconnect an already-connected player.
- An operator's own manual approve/deny from the dashboard (or a
  manually-added entry in the same tab's "Manual allowlist" card, for
  admins/testers/anyone without Discord) is always sticky — role-sync
  never overrides a human decision, only rows it manages itself.

### Phase 9 — VPS edge: public portal + no home port-forwarding (optional)

Everything above assumes players reach the cluster on your home network
directly. This phase adds a public VPS (Linode or otherwise) in front of
it instead — a WireGuard tunnel so nothing needs to be forwarded at home,
`folia-nexa-proxy` relocated onto the VPS as the actual public-facing
Minecraft port, Caddy for TLS, and the `folia-nexa-portal` player hub
snap (leaderboards/profiles/playtime/who's-online). `folia-nexa-mgmt`
itself **never moves** — it stays on the home network for all of this.

Full walkthrough: [`docs/vps-edge-deployment.md`](docs/vps-edge-deployment.md).
Supporting config lives in [`deploy/vps/`](deploy/vps/). Short version:

```bash
# On the VPS and the home LXD host (two-pass key exchange, see the doc):
sudo ./deploy/vps/setup-wireguard.sh --role vps
sudo ./deploy/vps/setup-wireguard.sh --role home --peer-public-key <...> --peer-endpoint <vps-ip>:51820
sudo ./deploy/vps/setup-wireguard.sh --role vps --peer-public-key <...>
sudo systemctl enable --now wg-quick@wg0   # both ends

# On the VPS: relocate folia-nexa-proxy here (FOLIA_MGMT_URL now points
# at mgmt's WireGuard-reachable address), then Caddy for TLS + routing:
sudo cp deploy/vps/Caddyfile /etc/caddy/Caddyfile   # filled in first
sudo systemctl reload caddy

# Build and install the portal snap (also on the VPS — build it wherever
# you build your other snaps, then copy the .snap over if that's not the
# VPS itself):
cd portal && snapcraft
sudo snap install ./folia-nexa-portal_0.1_amd64.snap --dangerous
sudo snap start folia-nexa-portal.daemon
```

The player hub's data (leaderboards, profiles) comes from a new mgmt-side
public API (`GET /api/v1/public/*`, `mgmt/src/folia_mgmt/routers/
public_stats.py`) fed by a new plugin, catalog id `FoliaNexaStats`
(`mgmt/src/folia_mgmt/catalog.yaml`) — as of this writing that catalog
entry is a placeholder pending the plugin's first real release; see the
entry's own `notes`.

## What's real vs. what's documented-but-unverified

Verified with real tooling in this repo's development (real HTTP
servers, real Gradle+Velocity API compilation, real running mgmt server
hit with curl/the CLI, real discord.py client/command-tree construction):

- Every mgmt API endpoint, the scheduler's placement/health-check/
  recovery/migration logic, the CLI, the dashboard.
- Host-level reachability checking (`scheduler.check_host_health`,
  `LXDClient.ping_host`): a real bug where a powered-off host stayed
  `online` in the dashboard forever (`Host.status` was only ever written
  at enrollment or by an operator's manual drain — nothing ever
  re-verified it) — every reconcile pass now pings each trusted host's
  own LXD API (`GET /1.0`) directly and flips `online`/`offline`
  accordingly, leaving operator-managed `draining`/`cordoned` hosts
  untouched. Unit-tested (`tests/test_scheduler.py`) and exercised
  end-to-end through the real reconcile loop against a running mgmt
  server (`tests/test_hosts.py`'s
  `test_powered_off_host_flips_to_offline_on_next_reconcile`) — not yet
  observed against a real LXD daemon actually going dark, only against
  `FakeLXDClient.ping_host` returning `False`.
- Draining a host now actually evacuates it (`scheduler.
  migrate_worlds_off_draining_hosts`) — `POST /hosts/{name}/drain` used to
  only stop *new* placements; a world already there just kept running on
  it forever, with no automatic migration despite PLAN.md §2's documented
  "host maintenance/decommission" intent for the status. Every reconcile
  pass now migrates every world still on a draining host (stop/export/
  import/start, same path as the existing manual per-world `POST
  /worlds/{name}/migrate`) to whichever online host has the most free
  capacity, and deletes the old container once the move succeeds; a world
  that can't be moved yet (no capacity) is retried the next pass. Unit-
  tested (`tests/test_scheduler.py`: best-fit target selection, crashed
  worlds get migrated rather than restarted in place, no-capacity and
  migration-failure retry paths, worlds on non-draining hosts left alone)
  and exercised end-to-end (`tests/test_hosts.py`'s
  `test_draining_a_host_migrates_its_worlds_elsewhere`, two reconcile
  passes through the real API: migrate, then finalize back to `running`
  on the new host) — same "written against `FakeLXDClient`, not a real
  LXD daemon" caveat as the rest of `LXDClient.migrate_container` above.
- The plugin catalog (`catalog.yaml` + override merge, `/api/v1/plugins`,
  world-creation validation, `/plugins-manifest` generation, the CLI's
  `plugins list`/`show`, and the dashboard's Plugins tab + world-creation
  picker) — hit end-to-end with curl against a real running mgmt server
  (login, list catalog, create a world with `plugins`, confirm the exact
  JSON body the dashboard's picker sends is accepted). The catalog's own
  data is a mix: `LuckPerms`, `Spark`, `BlueMap`, and `ServerSelector`
  have real, curl-verified (or downloaded-and-sha256'd, for
  `ServerSelector`) download URLs and checksums; the rest are
  placeholders (`download_url: null`) pending real vetting —
- The data pack catalog (`datapacks.yaml` + override merge,
  `/api/v1/datapacks`, world-creation validation, `/datapacks-manifest`
  generation, the CLI's `datapacks list`/`show`, and the dashboard's Data
  Packs tab + world-creation picker) — same pattern as the plugin catalog
  above, exercised end-to-end through the pytest suite (FastAPI's
  TestClient, not a manually-curled live server the way the plugin
  catalog note above was). Its one entry, `Matcha` ("Matcha Flavoured"),
  is real and verified 2026-08-15: fetched from Modrinth's API, the exact
  `download_url` above downloaded for real and sha256'd directly (Modrinth
  itself only publishes sha1/sha512 for this file). `folia-nexa-node`'s
  staging side (downloading a data pack manifest and placing zips under
  `<level-name>/datapacks/`) is unit-tested against a mocked HTTP
  transport only — not run against a real Folia/Paper server to confirm
  Matcha actually loads and applies at world generation; see the
  `LEVEL_NAME` note in `node/src/folia_node/staging.py` for the one
  assumption (`world`, the vanilla default level-name) this depends on,
  since this codebase never templates `server.properties`.
  `verified: false` in the catalog flags which is which.
- The lobby-as-hub design (PLAN.md §14B): `GET /api/v1/routes` preferring
  a running `lobby`-type world over an `overworld` for the `default`
  flag, and the `worlds delete`/`snapshot`/`restore` CLI commands it and
  `docs/game-master-howto.md` depend on — hit end-to-end against a real
  running mgmt server (declare a lobby and a minigame world, snapshot,
  confirm the clean 409 when snapshotting/restoring an unplaced world,
  delete). Not verified: `ServerSelector` actually switching a player
  between worlds in a real Velocity+Paper runtime — no live proxy/game
  client was available in this environment, only the plugin's own
  download and jar contents.
- `folia-routes-sync`'s routing diff, JSON parsing, and access-gate logic
  — compiled and unit-tested against the real `velocity-api` jar.
  `MgmtHttpFetcher` (the shared client behind routes polling, the access
  gate, the display API, and chat reporting) also self-heals from a
  wedged connection now: confirmed live 2026-08-17 on
  play.sullivan.linuxgroove.com that a pooled HTTP/1.1 connection can go
  silently black-holed (WireGuard itself never dropped — no `wg-quick`
  restart, no kernel link events, no VPS reboot in the outage window —
  but every poll timed out for over an hour until the proxy process was
  manually restarted). It now rebuilds its `HttpClient` after 3
  consecutive I/O failures rather than needing that manual restart;
  covered by `MgmtHttpFetcherTest`, not yet re-observed against a second
  real black-holed connection in production (the original incident is
  what motivated the fix, not a reproduction of it after the fact).
- `folia-nexa-bot`'s embed-building, auto-approve decision logic,
  Discord role-sync reconcile logic (`folia_bot/access.py`'s
  `role_membership_changed`/`compute_role_sync_ids`), and mgmt API
  client — unit-tested; `discord.Client`/`CommandTree` construction and
  command registration verified to build correctly against the real
  `discord.py` API. **The bot's actual gateway connection is now also
  confirmed live**, 2026-08-16: deployed to a real production host with
  a real bot token and the privileged `members` intent enabled in the
  Discord Developer Portal — connected to the gateway successfully, with
  no `PrivilegedIntentsRequired` crash, and began polling `GET
  /access-requests/discord-gate-config` on its own right after
  `on_ready`, exactly as designed. Not yet observed live: an actual
  `/request-access` command invocation by a real Discord member, or a
  real `on_member_update` gateway event actually firing the role-sync
  POST — only the connection itself and the periodic config poll were
  watched directly; the role-sync push logic is exercised by the unit
  tests above, not yet by a real Discord role change.
- `folia-host-join.sh`'s LXD-prep steps (project creation, trust token
  generation) — bash-syntax-checked and logically reviewed, not run
  against a live LXD daemon in this environment.
- `luckperms.py`'s config.yml rendering and the reconcile-loop sync logic
  that decides which worlds get it pushed.
- `db/bin/run-folia-nexa-db.sh` — actually run end-to-end (`mariadb-install-db`
  init, `mariadbd` startup, database/user bootstrap, a real client
  connection with the generated credentials, confirming root has no
  wildcard/remote grant) against the real MariaDB binaries extracted from
  Ubuntu's `.deb` packages. This caught two real path bugs (`mariadb-
  install-db` living under `usr/bin/`, not `usr/sbin/`; needing an
  explicit `--basedir` under a non-`/usr` prefix like a snap's `$SNAP`)
  before they'd have surfaced as a broken first boot. Not run inside
  actual snap confinement, though — see below.
- **All five `snapcraft.yaml` files, as real builds** — `snapcraft`
  9.0.1 against a real LXD-based build backend, every part actually
  fetching/compiling/staging, not a dry run. Caught two real bugs, both
  fixed: `proxy/`'s PaperMC API call was pointed at `api.papermc.io/v2`,
  which has been sunset in favor of `fill.papermc.io/v3` (different
  host, different response shape — this would have failed on literally
  the first real build attempt); `bot/`'s snap `summary` exceeded the
  78-character limit snap metadata enforces.
- The VPS edge (PLAN.md §7A): mgmt's new `POST /api/v1/stats/report` and
  `GET /api/v1/public/*` routers — real pytest suite, including the
  in-process cache and per-IP rate limiter. `portal/`'s three pages —
  loaded in a real headless Chromium against a real running mgmt
  instance seeded with real data, confirming leaderboard sorting, the
  player profile, and the playtime heatmap all render correctly.
  `deploy/vps/Caddyfile` — validated with real `caddy validate`.
  `deploy/vps/setup-wireguard.sh` — run end-to-end with real
  `wireguard-tools` across its full two-pass key-exchange flow,
  producing configs `wg-quick strip` parses cleanly. See
  `docs/vps-edge-deployment.md`'s own "what's real vs. unverified"
  section for the full breakdown.
- The portal's "who's online" view (`portal/online.html`,
  `mgmt/src/folia_mgmt/routers/presence.py`'s `POST
  /api/v1/presence/report` and `GET /api/v1/public/worlds`, and
  `FoliaRoutesSyncPlugin`'s new presence-reporting job in `proxy/`):
  real pytest coverage (`mgmt/tests/test_presence.py`,
  `test_public_stats.py`'s `test_worlds_online_*`) and a real Gradle
  build/test of the proxy-side JSON (`PresenceJsonTest`). The page itself
  was also exercised the same way as the rest of `portal/` — a real
  running `folia-nexa-mgmt`, seeded via real `POST
  /api/v1/presence/report` calls against real Mojang UUIDs, loaded in
  real headless Chromium — confirming the per-world player cards,
  avatars, and the stale-presence-falls-back-to-"0 online" behavior all
  render correctly from real API responses. See `portal/README.md`'s own
  "what's real" section. Not verified: a real Velocity proxy actually
  calling `RegisteredServer.getPlayersConnected()` and that report
  reaching a live mgmt instance — no live cluster was available in this
  environment, so the browser check above posted directly to the report
  endpoint in place of a real proxy.

Written against documented API contracts but **not** exercised against
live infrastructure:

- Every `LXDClient` method (mTLS bootstrap, instance CRUD, file push,
  backup export/import for migration) — a real LXD daemon exists in the
  environment this was developed in (used for the snap builds above),
  but nothing set up a `folia` project on it and pointed `LXDClient` at
  it for real — that's the next-most-valuable thing to verify if you're
  picking this project up.
- The Discord OAuth2 flow and Mojang UUID resolution — no registered
  Discord application was available to test the OAuth2 authorize/
  callback round trip against in this environment (the bot's gateway
  connection itself *is* now confirmed live — see above).
- `configs/luckperms/provision-mysql.sh` and the LuckPerms config.yml
  format itself (verify plugin config keys against whatever LuckPerms
  version you actually deploy — they can drift between major versions).
- Actually *installing* (`snap install --dangerous`) or running any of
  the five built snaps — no root/interactive-sudo access was available
  in this environment, only the build step. `db/`'s runtime behavior
  under real strict confinement specifically is the main open question:
  it could plausibly restrict something `mariadbd` wants (raw sockets,
  certain filesystem operations) that running the binary directly,
  unconfined, wouldn't catch.
- The VPS edge's actual network claims: a real WireGuard handshake
  across a real NAT'd home connection and a real public VPS IP, Let's
  Encrypt issuance against a real domain, and a real Minecraft client
  connecting through the relocated proxy — none of that is exercisable
  without real VPS + home hardware. The `FoliaNexaStats` plugin that
  feeds the player hub also doesn't exist yet as a real, released
  plugin — see `mgmt/src/folia_mgmt/catalog.yaml`'s entry for it.
- Bedrock client support (PLAN.md §7B): a real Bedrock/console/mobile
  client actually joining through Geyser-Velocity. This snap **has**
  now been built and run for real on a live production VPS
  (`play.sullivan.linuxgroove.com`), which caught a real incompatibility
  the original "fetch latest Geyser" approach missed: Geyser 2.11.1
  crashed at startup against Velocity 3.5.1 with `NoSuchMethodError:
  GsonComponentSerializer.toBuilder()` (Geyser relies on Velocity's own
  shaded `adventure` library rather than shading its own, and 2.11.1 was
  the first Geyser version whose compiled bytecode expected a method
  Velocity 3.5.1's bundled copy doesn't have — confirmed by disassembling
  `MessageTranslator.class` from several Geyser versions and the actual
  `GsonComponentSerializer`/`Buildable` classes inside `velocity-3.5.1.jar`
  itself, not just by reading a changelog). The RakNet listener silently
  never binds when this happens — Velocity itself stays up fine, so
  nothing about the crash is visible except an empty `:19132/udp` and a
  generic client-side timeout. The fix at the time was pinning
  `geyser-plugins` to Geyser 2.10.1 build 1184 instead of `latest` — the
  newest build confirmed (by the same bytecode check) to call the older,
  compatible `toBuilder` signature.

  That pin got a real, live confirmation the same day it shipped
  (2026-08-17): a real Bedrock client was disconnected with "Outdated
  Geyser proxy! This server supports ... 26.0 ... 26.33" — exactly
  2.10.1/1184's supported-version list, which proves that build *does*
  start and bind cleanly against Velocity 3.5.1 (a real client got a
  real, well-formed protocol response, not a timeout the way the 2.11.1
  crash looked). But it also exposed the pin's real cost: build 1184 is
  the *last* build ever published on the 2.10.x line — there's no newer
  2.10.x to move to, so any Bedrock client that auto-updates past 26.33
  is now permanently locked out regardless of how long the pin sits
  there.

  The actual fix, applied the same day: `velocity-runtime` was bumped
  from Velocity 3.5.1 to 4.0.0 (the only newer Velocity that exists —
  there's no 3.5.2/3.6.x, just a major-version jump), which also forced
  bumping the snap's bundled JRE from Java 21 to 25 (`velocity-api:4.0.0`
  requires it) and `routes-sync-plugin`'s own toolchain to match.
  `geyser-plugins` is re-pinned to 2.11.1 build 1225 (2026-08-16, the
  newest build as of this change, supports up to Bedrock 26.44) rather
  than `latest`, so a future Geyser build can't reintroduce a *different*
  incompatibility unnoticed. Verified for real: `folia-routes-sync`
  recompiles and its full 35-test suite still passes against
  `velocity-api:4.0.0` (`cd proxy && JAVA_HOME=~/.local/jdk21 ./gradlew
  build`); re-running the same bytecode check that found the original
  incompatibility — `javap` on the real `velocity-4.0.0-6.jar` — shows
  its shaded `GsonComponentSerializer` now directly declares the narrow
  `GsonComponentSerializer$Builder toBuilder()` signature (3.5.1's didn't;
  it only inherited the generic `Buildable.toBuilder()` that 2.10.1
  called), which disassembling Geyser 2.11.1 build 1225's
  `MessageTranslator` bytecode confirms is exactly the signature it now
  invokes — the same NoSuchMethodError class of bug, checked the same
  rigorous way, now resolved at the bytecode level.

  This *has* now been through a real `snapcraft` build + `snap install
  --dangerous` + start on `play.sullivan.linuxgroove.com` (2026-08-17),
  which caught one more real bug on top of the bytecode-level fix above:
  `run-velocity.sh` used to seed `velocity.jar` into `$SNAP_COMMON`
  once and never touch it again (the same "operator-editable, don't
  clobber it" treatment as `velocity.toml`), so the upgrade left the
  daemon running the *old* 3.5.1 `velocity.jar` against the *new*
  Geyser 2.11.1 plugin jar (which did reseed, since operators are meant
  to be able to force-refresh it) — hitting the exact
  `NoSuchMethodError: GsonComponentSerializer.toBuilder()` this bump
  was meant to fix, just one layer up the stack from where it was
  fixed. `velocity.jar` is now force-refreshed on every start, the same
  way `folia-routes-sync-*.jar` already was and for the identical
  reason (see that jar's own comment in `run-velocity.sh`). **Not yet
  confirmed**: this fixed `run-velocity.sh` actually being exercised by
  a real snap upgrade (the live incident above was caught and manually
  unblocked by deleting the stale `velocity.jar` before this script fix
  existed, not by the fixed script itself), and a real Bedrock client
  successfully reconnecting once Velocity is actually running 4.0.0.

If you're picking up this project to actually run it: those are the
places to validate first, roughly in that order.
