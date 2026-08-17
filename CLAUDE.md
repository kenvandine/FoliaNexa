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
| `portal/` | Public player hub (leaderboards, profiles, playtime heatmaps) — static site, no build step, deployed to the VPS edge (PLAN.md §7A) | HTML/CSS/vanilla JS |
| `deploy/vps/` | WireGuard tunnel + Caddy config for the VPS edge (PLAN.md §7A) — see `docs/vps-edge-deployment.md` | Bash, Caddyfile, WireGuard config templates |

In-house plugin source doesn't live in this repo — it's in the sibling
[`folianexa-plugins`](https://github.com/kenvandine/folianexa-plugins)
repo (one top-level directory per plugin, e.g. `campus-lobby/` for the
`CampusLobby` lobby-scene plugin), per `docs/plugin-dev/03-submitting-
for-review.md`. `catalog.yaml`'s `download_url` points at a release
built from there; this repo only ever needs that URL, not the source.

Each of `mgmt/`, `node/`, `proxy/`, `bot/`, `db/` is an independent,
independently testable component with its own `snapcraft.yaml`. **All
five build successfully with real `snapcraft` 9.0.1** (`snapcraft` from
each directory) — confirmed in this environment against a real LXD-based
build backend, not just reviewed. That build pass caught two real bugs
that a review wouldn't have: `proxy/snapcraft.yaml` was pointed at
`api.papermc.io/v2`, which PaperMC has since sunset in favor of
`fill.papermc.io/v3` (different host, different response shape); and
`bot/snapcraft.yaml`'s `summary` field exceeded snap metadata's 78-
character limit. Both fixed. (`proxy/snapcraft.yaml`'s later Bedrock/
GeyserMC addition — the `geyser-plugins` part, PLAN.md §7B — was
**not** re-verified against a real `snapcraft` build; no `snapcraft`/
`snapd` was available in the environment that change was made in. Its
two download URLs were fetched and sha256-checked directly instead —
see that part's comments.) `db/bin/run-folia-nexa-db.sh` was additionally
run end-to-end against the real MariaDB binaries (outside snap
confinement, extracted from the `.deb`s directly) — see its own comments
for what that did and didn't prove. None of the five have been
**installed** (`snap install ... --dangerous`) or actually started in
this environment — no root/interactive-sudo access was available for
that — so runtime behavior under real strict confinement is still
unverified; only the build step is confirmed.

## Running the test suites

```bash
# mgmt (Python) — 167 tests
cd mgmt && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q

# node (Python) — 17 tests
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
sudo snap install ./folia-nexa-proxy_3.5.1_amd64.snap --dangerous
```

It needs `FOLIA_MGMT_URL` and `FOLIA_MGMT_API_TOKEN` (an mgmt API token —
viewer role is enough) in `$SNAP_COMMON/proxy/config.env` before
starting; see `FoliaRoutesSyncPlugin`'s javadoc for the full env var
list, including the opt-in `FOLIA_ACCESS_GATE_ENABLED`.

The snap now also bundles Geyser-Velocity + floodgate-velocity, so
Bedrock (console/mobile/Win10) clients can join the same proxy on
`:19132/udp` with zero extra config beyond opening that port (see
"Bootstrapping" note in PLAN.md §7B, and Phase 9 below for the VPS-edge
firewall rule). This is new since the original snap build was last
confirmed against real `snapcraft` — the two GeyserMC download URLs the
new `geyser-plugins` part uses were fetched and sha256-verified for real,
but the full `snapcraft` build itself wasn't re-run with this addition
(no `snapcraft`/`snapd` available in the environment this was added in);
treat "confirmed to build" above as applying to everything except that
one new part until someone actually runs it.

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
Minecraft port, Caddy for TLS, and the `portal/` static player hub
(leaderboards/profiles/playtime). `folia-nexa-mgmt` itself **never
moves** — it stays on the home network for all of this.

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

# Deploy the static portal:
./deploy/vps/deploy-portal.sh --vps-host root@<vps-ip>
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
  client actually joining through Geyser-Velocity, and the full
  `snapcraft` build of `proxy/`'s new `geyser-plugins` part — neither a
  Bedrock client nor `snapcraft`/`snapd` was available in the
  environment this was added in. What *was* verified directly: both
  GeyserMC download URLs the new part uses resolve to real jars whose
  sha256 matches GeyserMC's own build-metadata API, and the optional
  `Floodgate` catalog entry's `download_url`/`sha256` the same way.

If you're picking up this project to actually run it: those are the
places to validate first, roughly in that order.
