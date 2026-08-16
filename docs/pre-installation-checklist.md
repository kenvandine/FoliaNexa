# Pre-installation checklist

A condensed, operator-facing checklist to work through *before* starting
CLAUDE.md's Phase 0–9 bootstrap for real. It exists because several
things in this repo are documented as real-but-unverified (see CLAUDE.md's
"What's real vs. what's documented-but-unverified") or ship with
deliberate placeholders — this page collects the ones that will actually
block or silently degrade a first install, so you hit them here instead
of mid-run tonight.

## 1. Things that will silently do less than you expect

- **Most of the survival world's plugin list has no real download yet.**
  `configs/worlds/create-survival.sh` declares 14 `--plugin` entries;
  9 of them (`HuskClaims`, `Vault-Unlocked`, `AxAuctions`, `AuraSkills`,
  `ItemsAdder`, `MythicMobs`, `HuskPortals`, `SimpleVoiceChat`,
  `DiscordSRV`) are `download_url: null` placeholders in
  `mgmt/src/folia_mgmt/catalog.yaml`. A plugin with no `download_url` is
  **silently skipped** in the generated manifest — not an error, not a
  warning in the CLI. Only `LuckPerms`, `Spark`, `BlueMap`, `HuskHomes`
  (verified 2026-08-16, see below), and `FoliaNexaStats` will actually
  land in that world's `plugins/` folder as-is. Same story for
  `SkyWarsReloaded`/`BedWars1058` on both minigame worlds — both fully
  placeholder, so those two worlds will boot with **zero** minigame
  logic until you resolve real URLs.
  - Decide tonight: run `create-all.sh` anyway (accept an unfinished
    plugin loadout to first confirm the pipeline end-to-end), or fill in
    real `download_url`/`sha256` values first via
    `plugin-catalog-override.yaml` in mgmt's state dir. `folia-nexa-mgmt
    plugins list` / `plugins show <id>` will tell you which ids are
    still `verified: false`.
- **`HuskClaims` is a paid plugin, checked for real 2026-08-16** —
  £9.99 via SpigotMC/Polymart/BuiltByBit purchase gates (william278.net's
  own project page confirms the pricing), not on Modrinth/Hangar, and
  its GitHub releases carry no uploaded jar (only the automatic source
  archives). Same category as `ItemsAdder`: it will never get a plain
  public `download_url`. If claims matter for tonight's survival world,
  budget for buying a license and self-hosting the jar, or drop it from
  `create-survival.sh` and pick a free alternative.
- **`HuskHomes` is now real** — verified 2026-08-16 directly against
  Modrinth's API and re-downloaded/sha256'd for real (`catalog.yaml`'s
  entry has the details). It's free, unlike `HuskClaims`, and the pinned
  build (4.11) supports this cluster's default engine version (1.21.4).
  One fewer plugin to worry about in the list above.
- **`ItemsAdder` will never get a plain URL** — it's SpigotMC
  license-gated. Either drop it from `create-survival.sh` or plan to
  self-host a licensed copy.
- Minigame plugins (`SkyWarsReloaded`, `BedWars1058`) are **not verified
  Folia-compatible** even once you find a download — classic Bukkit-era
  plugins often assume single-threaded world ticking Folia doesn't
  guarantee. Worth a quick check against your Folia build before
  trusting them in production, not just before install.

## 2. Environment variables you must set before Phase 5 (`worlds create`)

- `FOLIA_MGMT_ARTIFACTS_BASE_URL` (default `https://artifacts.internal`,
  almost certainly wrong for you) — must serve
  `{artifacts_base_url}/folia/1.21.4/folia.jar` (default engine/version
  from `worlds create --engine/--version`, currently `folia`/`1.21.4`)
  reachable from every LXD host, or every world's `folia-nexa-node`
  fails to fetch its engine jar on first boot. Have this actually hosting
  a real Folia jar before you run `create-all.sh`, not after.
- `FOLIA_MGMT_PUBLIC_URL` — required for *any* `--plugin`/`--datapack`
  declaration (worlds fetch their manifest from
  `{public_url}/api/v1/worlds/{name}/plugins-manifest` and
  `.../datapacks-manifest`). Must be mgmt's own reachable address from
  every LXD host's point of view, not `localhost`.
- If any world (survival does) includes `LuckPerms`, the four
  `FOLIA_MGMT_LUCKPERMS_MYSQL_*` vars (`HOST`, `PORT`, `DATABASE`,
  `USER`, `PASSWORD`) need to be set on the mgmt host **before** those
  worlds first start — Phase 6, and it's easy to do this after Phase 5
  since nothing forces the ordering. LuckPerms reads its storage backend
  at plugin load time, not live, so a world started before these are set
  needs a restart to pick them up.

## 3. Installation steps that are unverified in *this* environment

These build/compile cleanly (see CLAUDE.md for exactly what was and
wasn't confirmed) but nobody has run the actual `snap install` /
`snap start` / live-daemon steps against them here — root and a real
snapd weren't available in the sandbox this was developed in. Budget
time tonight for first-run surprises specifically at these steps:

- `snap install ./folia-nexa-*.snap --dangerous` for all five snaps —
  build-verified, install/runtime-under-confinement is not.
- `folia-nexa-db`'s runtime **under actual strict snap confinement**
  specifically — the MariaDB binaries were run end-to-end unconfined
  (extracted from `.deb`s), which proved the init/bootstrap logic but
  not that strict confinement won't block something `mariadbd` wants
  (raw sockets, certain filesystem calls).
- `folia-nexa-proxy`'s new `geyser-plugins` snapcraft part (Bedrock
  support) — the two GeyserMC/Floodgate download URLs were fetched and
  sha256-verified directly, but the part itself was never run through a
  real `snapcraft` build (no snapcraft/snapd in the environment that
  added it). If `cd proxy && snapcraft` fails tonight, this part is the
  first place to look.
- `LXDClient`'s actual calls against a live LXD daemon (mTLS bootstrap,
  instance CRUD, file push, backup export/import) — written against
  LXD's documented API, never pointed at a real `folia` project on a
  real daemon. This is the biggest unknown in the whole bootstrap: Phase
  3's `folia-host-join.sh` trust flow and Phase 5's `worlds create`
  actually placing a container both exercise this path for the first
  time tonight.
- `folia-host-join.sh` itself — bash-syntax-checked and reviewed, not
  run against a live LXD daemon.

## 4. Ordering / sequencing gotchas

- `bootstrap-admin` only works run *on* the mgmt host itself (talks to
  the local DB directly, no HTTP) — don't try it remotely.
- The `folia-nexa-proxy` service account (Phase 7) needs mgmt's API
  already up and a bearer token in hand — create it with `curl` against
  `/api/v1/users` before trying to start the proxy snap, or it'll start
  with no valid `FOLIA_MGMT_API_TOKEN` and fail to sync routes.
- If you're doing the VPS edge (Phase 9) tonight too: `folia-nexa-mgmt`
  itself never moves off the home network — only the proxy relocates.
  Confirm the WireGuard tunnel is up (`wg show`) *before* pointing the
  relocated proxy's `FOLIA_MGMT_URL` at mgmt's tunnel address, or you'll
  chase a DNS/connectivity red herring instead of the real WireGuard
  problem.

## 5. Quick pre-flight commands

Run these before touching `snapcraft` tonight, so build failures don't
eat your first hour:

```bash
# JDK 21+ on PATH for the proxy build
java -version || echo "no system JDK — see CLAUDE.md's portable-JDK snippet"

# snapd + LXD present and lxd already initialized
snap list lxd && lxc storage list

# snapcraft itself
snapcraft version

# confirm your artifacts server actually serves the engine jar
curl -fsSL -o /dev/null "$FOLIA_MGMT_ARTIFACTS_BASE_URL/folia/1.21.4/folia.jar" \
  && echo OK || echo "engine jar not reachable — worlds will fail to boot"
```

## 6. If something breaks

Cross-reference CLAUDE.md's "What's real vs. what's documented-but-
unverified" section — if the thing that broke is listed there as
unverified, it's a known gap to debug from scratch, not a regression.
The two real bugs the last full `snapcraft` pass caught
(`api.papermc.io/v2` → `fill.papermc.io/v3` in `proxy/`, and `bot/`'s
78-character `summary` limit) are already fixed, so if you hit either of
those symptoms again, check you're actually on this branch's latest
`snapcraft.yaml`.
