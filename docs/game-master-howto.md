# Game Master How-To: Designing and Deploying a Minigame World

This is a task-oriented guide for the person designing a minigame (or any
new world) and getting it live on the cluster — not a description of how
the underlying system works. For that, see `PLAN.md` (architecture) and
`CLAUDE.md` (bootstrap/ops runbook). This doc assumes both already exist:
a running `folia-nexa-mgmt`, at least one trusted host with capacity, and
a `folia-nexa-proxy` in front of it.

There's no separate "game master" account type — it's an `operator`-role
`folia-nexa-mgmt` user (`viewer` can look but not create/delete/snapshot;
`admin` additionally manages users and hosts). Get an operator account
from whoever runs the cluster, then `folia-nexa-mgmt login <mgmt-url>
<username> <password>` before any of the CLI commands below.

## 1. Design the world

Decide, up front, the four things a world declaration needs:

- **A name.** Stable and permanent for that world's lifetime — it's the
  LXD container name, the Velocity backend server name, and (if you use
  the ServerSelector plugin, §5) the id players click on in the lobby
  menu. `world-minigame-<something>` matches the existing convention in
  `configs/worlds/`.
- **A type.** `minigame` (there's also `overworld`, `nether`, `end`,
  `lobby`, `proxy`, `staging`, `infra` — see `PLAN.md` §2). Type mostly
  affects placement labels and the lobby's default-routing logic (§14B),
  not JVM behavior directly.
- **Sizing.** `--cpu` and `--memory`. The two starter minigames in
  `configs/worlds/` (SkyWars, BedWars) run at 2 CPU / 3GB on an E-core —
  minigames are typically much lighter than a persistent survival
  overworld (6 CPU / 12GB in the same starter set). Undersizing just
  means laggy gameplay, not a hard failure; there's no minimum enforced.
- **Placement labels**, if your hosts are heterogeneous (e.g. P-core vs
  E-core machines, PLAN.md §5) and you want this world scheduled onto a
  specific kind — `--labels cpu_type=e-core`. Optional; omit for "any
  host with capacity."

## 2. Pick your plugins from the catalog

Every plugin a world declares must already be a `folia-nexa-mgmt`
catalog entry — free-typed plugin names are rejected at world-creation
time (PLAN.md §14). Browse what's available:

```bash
folia-nexa-mgmt plugins list
folia-nexa-mgmt plugins list --category minigame
folia-nexa-mgmt plugins show LuckPerms
```

Or use the dashboard's **Plugins** tab, or the checkbox picker on the
**Declare a world** form — same data either way.

**If the plugin you want isn't in the catalog yet:** check with whoever
runs the cluster about adding it to `mgmt/src/folia_mgmt/catalog.yaml`
(a PR, since it's repo-tracked and code-reviewed), or — if you have
access to the mgmt host — drop it into a
`$SNAP_COMMON/plugin-catalog-override.yaml` yourself without needing a
new mgmt release (PLAN.md §14A). This is also how an in-house minigame
plugin from your own repository gets in: the catalog only needs a
`download_url` pointing at wherever you publish releases (your own
GitHub releases, an internal artifact host — anywhere reachable over
HTTP), an `id`, and a real `sha256` once you've downloaded and hashed
the jar yourself — see any `source: external` entry in `catalog.yaml`
for the shape (`category`/`source` just become `in-house` for a plugin
of your own).

**Two things worth checking before you commit to a plugin:**

- `folia-nexa-mgmt plugins show <id>` and look at `verified` /
  `download_url`. A `download_url: null` entry is a placeholder — your
  world will stage fine but that one plugin gets silently skipped (not
  an error) when node fetches the manifest. Get the entry a real URL
  first (catalog.yaml or an override) if you actually need it running.
- Classic Bukkit-era minigame plugins (kit PvP, arena managers, that
  kind of thing) commonly assume single-threaded world ticking, which
  Folia's regionized scheduler doesn't guarantee. Check the plugin's own
  Folia-compatibility claims before relying on it in production — the
  `SkyWarsReloaded`/`BedWars1058` catalog entries carry this same
  caveat in their `notes`.

**Bedrock players on this world:** the cluster's proxy already lets
Bedrock (console/mobile/Win10) clients join by default (PLAN.md §7B) —
you don't need to do anything for a Bedrock player to reach your world.
Add `--plugin Floodgate` only if *this specific world* should recognize
who joined via Bedrock (correct skin/identity here, or a plugin reading
the Floodgate API) — see the `Floodgate` catalog entry's `notes` for the
one manual setup step it needs (copying a key file from the proxy).

## 3. Declare and deploy the world

```bash
folia-nexa-mgmt worlds create world-minigame-parkour \
  --type minigame \
  --cpu 2 \
  --memory 3GB \
  --labels cpu_type=e-core \
  --plugin LuckPerms \
  --plugin Spark
```

(Or the dashboard's **Declare a world** form — same fields, plus the
plugin checkbox picker instead of `--plugin`.) This only works if
`FOLIA_MGMT_PUBLIC_URL` is configured on the mgmt server — required as
soon as any world declares plugins, since that's the address worlds use
to fetch their manifest (PLAN.md §14A). If you get a 400 mentioning
`PUBLIC_URL`, that's an mgmt-server config gap, not something fixable
from the CLI — flag it to whoever runs the cluster.

Watch it come up:

```bash
folia-nexa-mgmt worlds list
```

`phase` goes `pending` → `provisioning` → `running` as mgmt places it on
a host, launches the container, and node stages the jar + plugins and
starts the JVM. `pending` that never advances usually means no host has
enough free capacity right now (`folia-nexa-mgmt hosts list` to check) —
it'll place automatically the moment capacity frees up, no re-run
needed. A `plugins-manifest` you can inspect directly if you want to
confirm exactly what node is about to fetch:

```bash
curl https://<mgmt-host>:8443/api/v1/worlds/world-minigame-parkour/plugins-manifest
```

(unauthenticated by design — it's the same URL node itself calls.)

## 4. Confirm it's reachable

Once `phase` is `running`, `folia-nexa-proxy` picks it up on its next
poll (every few seconds, PLAN.md §7) and registers it as a Velocity
backend under the world's name — no proxy restart. `folia-nexa-mgmt
worlds list` shows the assigned address once placed; you can connect
directly to `<address>:25565` to test before wiring it into the lobby.

## 5. Make it choosable from the lobby

This is the point of having a lobby at all — see PLAN.md §14B for the
full design. Two ways players get from the lobby to your new world,
neither requiring anything on the proxy itself:

**Zero-plugin path (always works):** every player connected through the
proxy can run `/server world-minigame-parkour` — Velocity's built-in
command, tab-completed against whatever's currently registered. Fine
for testing or a small, technical playerbase; not very discoverable for
a general audience.

**GUI path (catalog's `ServerSelector` entry):** a small Paper plugin
installed **on the lobby world itself** (not the proxy — it talks to
Velocity over the standard BungeeCord/Velocity plugin-messaging
channel, which needs no proxy-side config). Add it to the lobby world's
plugin list, then edit its `config.yml` to add an entry whose `id:` is
your world's name. Do this from the dashboard's "Plugin configs" button
on the lobby world's row (pick `ServerSelector`, edit `config.yml`,
Save), or from the CLI:

```bash
folia-nexa-mgmt worlds plugin-config show world-lobby ServerSelector config.yml > /tmp/selector-config.yml
# edit /tmp/selector-config.yml, then:
folia-nexa-mgmt worlds plugin-config set world-lobby ServerSelector config.yml --file /tmp/selector-config.yml
```

Add an entry whose `id:` is your world's name:

```yaml
servers:
  parkour:
    material: FIREWORK_ROCKET
    name: "&e&lParkour"
    lore:
      - "&7Race the clock"
    id: world-minigame-parkour   # must match the world name exactly
    slot: 15
```

Either path saves the edit in mgmt (so it survives restarts/migrations,
re-applied every reconcile tick) and pushes it live immediately if the
world is reachable; `/selector reload` on the lobby world picks up the
change without a restart either way. It's a small, single-maintainer
plugin (see its catalog `notes`) — the download itself is verified, but
test it before an event rather than trusting it blind at scale.

## 6. Iterating on the design

**Changing plugins after creation:** there's no "update a world's
plugin list" API — `world.plugins` is set once at creation and node
only stages plugins the first time a container boots (an idempotent
`.staged` marker skips re-fetching on ordinary restarts, PLAN.md §9).
To change the loadout, delete and recreate the world:

```bash
folia-nexa-mgmt worlds create world-minigame-parkour --type minigame --cpu 2 --memory 3GB \
  --plugin LuckPerms --plugin Spark --plugin FancyHolograms
```

after deleting the old one — see below. If the world has player-created
state you care about (unlikely for most minigame arenas, which are
usually regenerated per round anyway, but possible), snapshot first.

**Changing an existing plugin's config, without recreating the world:**
unlike the plugin *list*, an already-declared plugin's config *files*
can be edited in place — the dashboard's "Plugin configs" button per
world row, or `folia-nexa-mgmt worlds plugin-config
{list,show,set,revert}` from the CLI (same commands as the lobby
`ServerSelector` example in §5, generalized to any plugin). mgmt saves
the edit as the source of truth and re-pushes it every reconcile tick,
same as `world.plugins` itself, so it survives restarts. Most plugins
only read their config at boot, though — save an edit, then `folia-nexa-mgmt
worlds restart world-minigame-parkour` (or the dashboard's "Restart
world to apply" button in that same modal) for it to actually take
effect. `LuckPerms` and `FoliaNexaStats` are the two exceptions: mgmt
renders and manages their `config.yml` itself from live cluster secrets
(the shared MySQL backend, an API token) and the file browser refuses
to show or accept edits to either — nothing to configure there by hand.

**Snapshotting / restoring:**

```bash
folia-nexa-mgmt worlds snapshot world-minigame-parkour            # optionally: --snapshot-name before-event
folia-nexa-mgmt worlds restore world-minigame-parkour before-event
```

(dashboard has a "Snapshot" button per world too, for the snapshot half).

**Tearing down** when an event's over or a design didn't work out:

```bash
folia-nexa-mgmt worlds delete world-minigame-parkour
```

This deletes the underlying LXD container — unrecoverable unless you
snapshotted first. `folia-routes-sync` drops it from the proxy's backend
list on its next poll; remember to also remove its entry from the
lobby's `ServerSelector` config so players don't get sent to a dead
route.

---

## Appendix: configuring the lobby itself

The lobby is a world like any other — declare it with `--type lobby`:

```bash
folia-nexa-mgmt worlds create world-lobby --type lobby --cpu 1 --memory 1GB \
  --plugin LuckPerms --plugin ServerSelector
```

Two things make it behave like a hub rather than just another world:

- **It becomes the default landing point automatically.** `GET
  /api/v1/routes` flags exactly one route `default: true`, and mgmt
  prefers a running `lobby`-type world over a running `overworld` for
  that flag (PLAN.md §14B) — new player connections land there with no
  proxy config change. Confirm it took effect:
  ```bash
  curl -H "Authorization: Bearer <token>" https://<mgmt-host>:8443/api/v1/routes
  ```
  and check `"default": true` is on `world-lobby`, not whatever
  overworld you also run.
- **It's where `ServerSelector` (or plain `/server`) lives.** Install
  `ServerSelector` on the lobby world specifically (not on every world,
  not on the proxy) and keep its `config.yml` in sync with whatever
  games are currently live — that config file is the actual "menu" a
  game master maintains day to day. `FancyNpcs`/`FancyHolograms` (both
  catalog placeholders — see their `notes`, need a real `download_url`
  before use) are common companions for lobby decoration (floating
  leaderboards, NPC greeters) if you want to go further than a compass
  menu.

Keep the lobby itself small and boring — it's not where gameplay
happens, it's the front door. Over-plugin-ing it just adds another
surface that can break the one world every player passes through.
