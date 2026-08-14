Here is the full, unabridged Markdown document with all sections, code files, configurations, and examples in one place.

---

```markdown
# Folia 250-Player SMP Cluster: Complete Project Specification & Implementation Plan

**Target Host:** Intel Core Ultra 5 235T (14C/14T: 6P + 8E), 32 GB DDR5 RAM, NVMe Storage  
**Alternative Benchmark:** Intel Core i9-14900T (24C/32T: 8P + 16E)  
**Base OS:** Ubuntu Server 24.04 LTS with LXD System Containers  
**Application Packaging:** Snaps with `systemd` daemon supervision  
**Control Plane:** Custom `mc-cluster-manager` Web Dashboard & REST API  

---

## 1. System Architecture & Topology

The deployment decouples edge routing, world execution, data persistence, and cluster management into isolated LXD containers connected over an internal virtual software network bridge (`lxdbr0` or OVN).


```

```
                         [ Public Internet ]
                                  │
                          (Port 25565/TCP)
                                  ▼
         ┌──────────────────────────────────────────────────┐
         │       LXD Container: edge-proxy (1 E-Core)       │
         │       Snap: velocity-proxy (daemon: simple)      │
         └────────────────────────┬─────────────────────────┘
                                  │
                (Internal OVN / lxdbr0 Bridge)
                                  │
     ┌────────────────────────────┼────────────────────────────┐
     ▼                            ▼                            ▼

```

┌──────────────────┐        ┌──────────────────┐         ┌──────────────────┐
│ LXD: folia-smp   │        │ LXD: folia-nether│         │ LXD: hub-lobby   │
│ Snap: folia-node │        │ Snap: folia-node │         │ Snap: folia-node │
│ (Pinned P-Cores) │        │ (Pinned E-Cores) │         │ (Pinned E-Cores) │
└────────┬─────────┘        └────────┬─────────┘         └────────┬─────────┘
│                           │                            │
└───────────────────────────┼────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────┐
│       LXD Container: mgmt-control                │
│       Snap: mc-cluster-manager (Web UI & API)    │
│       • Talks to /var/snap/lxd/common/lxd/unix   │
│       • Dynamic Velocity Proxy Route Sync        │
│       • Ephemeral Staging Testbed Engine         │
└──────────────────────────────────────────────────┘

```

---

## 2. Hardware Allocation & Core Pinning Strategy

### A. Target Baseline: Intel Core Ultra 5 235T (14 Cores / 14 Threads)
* 6 Lion Cove Performance Cores (P-Cores, up to 5.0 GHz)
* 8 Skymont Efficient Cores (E-Cores, up to 3.6 GHz)

| Service / Container | Core Assignment | Core Type | Memory Cap | Target Role |
| :--- | :--- | :--- | :--- | :--- |
| **`folia-smp`** | `0,1,2,3,4,5` | 6 P-Cores | 12 GB RAM | Main 250-player Overworld SMP |
| **`folia-nether-end`**| `6,7,8,9` | 4 E-Cores | 6 GB RAM | Offloaded Nether & The End dimensions |
| **`hub-lobby`** | `10,11` | 2 E-Cores | 3 GB RAM | Player spawn, queueing, fallback hub |
| **`edge-proxy`** | `12` | 1 E-Core | 2 GB RAM | Velocity proxy + rate limiting |
| **`mgmt-control`** | `13` | 1 E-Core | 2 GB RAM | Web dashboard & orchestration daemon |
| **Host / ZFS Headroom**| Dynamic | All Cores | ~7 GB RAM | Host kernel, ZFS ARC, snapshot COW operations |

### B. Scaled Alternative: Intel Core i9-14900T (24 Cores / 32 Threads)
* 8 Raptor Cove P-Cores (16 Threads, up to 5.5 GHz)
* 16 Gracemont E-Cores (16 Threads, up to 3.9 GHz)
* **Allocation Advantage:** Dedicated 8 physical P-cores (16 threads) strictly to `folia-smp` (handling 350–400+ concurrent players across multiple active regions), with 16 dedicated E-cores powering all auxiliary containers, live BlueMap 3D rendering, and Redis/PostgreSQL persistence.

---

## 3. Snap Packaging Specifications

Packaging server runtimes into Snaps guarantees immutable binaries, clean transactional updates via channels, and automated service supervision via `snapd`. Mutable state lives under `$SNAP_COMMON` (`/var/snap/<snap-name>/common/`).

### A. `folia-server` Snap Recipe (`snapcraft.yaml`)
```yaml
name: folia-server
version: '1.21'
summary: Folia Multithreaded Dedicated Server Daemon
description: Regionized multithreading Minecraft server engine based on PaperMC.
base: core24
confinement: strict

environment:
  JAVA_HOME: $SNAP/usr/lib/jvm/java-21-openjdk-amd64
  PATH: $SNAP/usr/lib/jvm/java-21-openjdk-amd64/bin:$PATH
  SERVER_DIR: $SNAP_COMMON/server

apps:
  daemon:
    command: bin/run-folia.sh
    daemon: simple
    restart-condition: on-failure
    restart-delay: 5s
    plugs:
      - network
      - network-bind
      - mount-observe

parts:
  folia-runtime:
    plugin: dump
    source: .
    stage-packages:
      - openjdk-21-jre-headless
    override-build: |
      craftctl default
      mkdir -p $CRAFT_PART_INSTALL/bin
      cat << 'EOF' > $CRAFT_PART_INSTALL/bin/run-folia.sh
      #!/bin/bash
      mkdir -p "$SERVER_DIR"
      cd "$SERVER_DIR"
      
      # Regionized Multithreading Heap & Generational ZGC Flags
      exec java -Xms${JVM_MIN_RAM:-4G} -Xmx${JVM_MAX_RAM:-10G} \
        -XX:+UseZGC -XX:+ZGenerational \
        -XX:+AlwaysPreTouch \
        -Dterminal.jline=false \
        -Dterminal.ansi=true \
        -jar $SNAP/folia.jar --nogui
      EOF
      chmod +x $CRAFT_PART_INSTALL/bin/run-folia.sh

```

### B. `velocity-proxy` Snap Recipe (`snapcraft.yaml`)

```yaml
name: velocity-proxy
version: '3.3.0'
summary: Velocity Next-Gen Proxy Daemon
description: High-performance proxy for horizontal Minecraft server orchestration.
base: core24
confinement: strict

environment:
  JAVA_HOME: $SNAP/usr/lib/jvm/java-21-openjdk-amd64
  PATH: $SNAP/usr/lib/jvm/java-21-openjdk-amd64/bin:$PATH

apps:
  daemon:
    command: bin/run-velocity.sh
    daemon: simple
    restart-condition: always
    plugs:
      - network
      - network-bind

parts:
  velocity-runtime:
    plugin: dump
    source: .
    stage-packages:
      - openjdk-21-jre-headless
    override-build: |
      craftctl default
      mkdir -p $CRAFT_PART_INSTALL/bin
      cat << 'EOF' > $CRAFT_PART_INSTALL/bin/run-velocity.sh
      #!/bin/bash
      mkdir -p "$SNAP_COMMON/proxy"
      cd "$SNAP_COMMON/proxy"
      exec java -Xms1G -Xmx2G -XX:+UseG1GC -jar $SNAP/velocity.jar
      EOF
      chmod +x $CRAFT_PART_INSTALL/bin/run-velocity.sh

```

---

## 4. Curated Plugin Matrix (Folia-Supported)

All plugins must target Folia's `RegionScheduler` and `GlobalRegionScheduler` to prevent single-thread bottlenecks:

| Category | Plugin Name | Role & Feature |
| --- | --- | --- |
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

## 5. LXD Profiles & Configuration

```bash
# 1. Base Shared Profile
lxc profile create folia-base
lxc profile edit folia-base << 'EOF'
config:
  security.nesting: "true"
  limits.memory.enforce: "hard"
devices:
  eth0:
    name: eth0
    network: lxdbr0
    type: nic
  root:
    path: /
    pool: default
    type: disk
EOF

# 2. Main Overworld SMP Profile (P-Cores 0-5, 12GB RAM)
lxc profile create folia-smp-main
lxc profile set folia-smp-main limits.cpu "0-5"
lxc profile set folia-smp-main limits.memory "12GB"

# 3. Nether/End Sub-World Profile (E-Cores 6-9, 6GB RAM)
lxc profile create folia-subworld
lxc profile set folia-subworld limits.cpu "6-9"
lxc profile set folia-subworld limits.memory "6GB"

# 4. Ephemeral Staging Profile (E-Cores 6-7, 6GB RAM, Low Priority)
lxc profile create folia-staging
lxc profile set folia-staging limits.cpu "6-7"
lxc profile set folia-staging limits.memory "6GB"
lxc profile set folia-staging limits.cpu.priority "1"

# 5. Edge Proxy Profile (E-Core 12, 2GB RAM)
lxc profile create proxy-edge
lxc profile set proxy-edge limits.cpu "12"
lxc profile set proxy-edge limits.memory "2GB"

```

---

## 6. Staging Testbed & Promotion Automation

### A. Staging Provisioning Script (`/usr/local/bin/provision_staging.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "==> Capturing ZFS snapshot of production SMP..."
lxc snapshot folia-smp staging-base

echo "==> Cloning snapshot into ephemeral container 'folia-staging'..."
lxc copy folia-smp/staging-base folia-staging -p folia-base -p folia-staging

echo "==> Patching internal port to 25570..."
lxc exec folia-staging -- sed -i 's/server-port=25565/server-port=25570/g' /var/snap/folia-server/common/server/server.properties

echo "==> Starting staging container..."
lxc start folia-staging

echo "==> Staging environment ready on internal port 25570."

```

### B. Plugin Promotion Script (`/usr/local/bin/promote_plugin.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

PLUGIN_PATH="$1"
TARGET_CONTAINER="folia-smp"
PLUGIN_DEST="/var/snap/folia-server/common/server/plugins"

# Validate Folia compatibility in manifest
if ! unzip -p "$PLUGIN_PATH" plugin.yml | grep -q "folia-supported: true"; then
  echo "ERROR: Plugin does not declare 'folia-supported: true' in plugin.yml!"
  exit 1
fi

echo "==> Taking safety pre-promotion snapshot..."
lxc snapshot "${TARGET_CONTAINER}" "pre-plugin-$(date +%Y%m%d%H%M%S)"

echo "==> Injecting validated plugin into production..."
lxc file push "$PLUGIN_PATH" "${TARGET_CONTAINER}${PLUGIN_DEST}/"

echo "==> Restarting Folia daemon supervisor..."
lxc exec "${TARGET_CONTAINER}" -- snap restart folia-server.daemon

echo "==> Plugin successfully promoted to production."

```

---

## 7. Web Management Control Plane (`mc-cluster-manager`)

### A. FastAPI Backend (`main.py`)

```python
import os
import subprocess
import zipfile
from fastapi import FastAPI, UploadFile, File, HTTPException
import pylxd

app = FastAPI(title="MC Cluster Control Plane")
client = pylxd.Client()

PLUGIN_STAGING_DIR = "/tmp/plugin_staging"
os.makedirs(PLUGIN_STAGING_DIR, exist_ok=True)

@app.get("/api/containers")
def list_containers():
    containers = []
    for c in client.containers.all():
        if "folia" in c.name or "proxy" in c.name:
            state = c.state()
            containers.append({
                "name": c.name,
                "status": c.status,
                "cpu_usage": state.cpu.usage,
                "memory_used_mb": round(state.memory.usage / (1024 * 1024), 2),
                "profiles": c.profiles
            })
    return {"containers": containers}

@app.post("/api/staging/spin-up")
def create_staging():
    try:
        subprocess.run(["/usr/local/bin/provision_staging.sh"], check=True)
        return {"status": "success", "message": "Staging testbed online at port 25570"}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/staging/destroy")
def destroy_staging():
    try:
        c = client.containers.get("folia-staging")
        c.stop(wait=True)
        c.delete(wait=True)
        return {"status": "success", "message": "Staging testbed destroyed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/staging/upload-plugin")
async def upload_plugin(file: UploadFile = File(...)):
    file_location = os.path.join(PLUGIN_STAGING_DIR, file.filename)
    with open(file_location, "wb") as f:
        f.write(await file.read())

    try:
        with zipfile.ZipFile(file_location, 'r') as z:
            manifest = z.read('plugin.yml').decode('utf-8')
            if 'folia-supported: true' not in manifest:
                os.remove(file_location)
                raise HTTPException(status_code=400, detail="Plugin does not declare 'folia-supported: true'")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid plugin JAR or corrupted manifest.")

    subprocess.run([
        "lxc", "file", "push", file_location,
        "folia-staging/var/snap/folia-server/common/server/plugins/"
    ], check=True)
    subprocess.run(["lxc", "exec", "folia-staging", "--", "snap", "restart", "folia-server.daemon"], check=True)

    return {"status": "success", "filename": file.filename, "message": "Plugin staged and testbed restarted."}

@app.post("/api/production/promote/{filename}")
def promote_to_production(filename: str):
    file_location = os.path.join(PLUGIN_STAGING_DIR, filename)
    if not os.path.exists(file_location):
        raise HTTPException(status_code=404, detail="Staged plugin file not found.")

    try:
        subprocess.run(["/usr/local/bin/promote_plugin.sh", file_location], check=True)
        return {"status": "success", "message": f"{filename} successfully promoted to production."}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=str(e))

```

### B. Control Plane HTML5 Dashboard (`index.html`)

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>MC Cluster Control Dashboard</title>
  <script src="[https://cdn.tailwindcss.com](https://cdn.tailwindcss.com)"></script>
</head>
<body class="bg-slate-900 text-slate-100 p-8 font-sans">
  <div class="max-w-6xl mx-auto space-y-6">
    <header class="flex justify-between items-center border-b border-slate-700 pb-4">
      <h1 class="text-2xl font-bold text-emerald-400">⚡ Folia Cluster Manager</h1>
      <span class="text-sm bg-slate-800 px-3 py-1 rounded border border-slate-700">Host: Intel Core Ultra 5 235T</span>
    </header>

    <section class="grid grid-cols-1 md:grid-cols-4 gap-4">
      <div class="bg-slate-800 p-4 rounded border border-slate-700">
        <h3 class="text-xs font-semibold text-slate-400 uppercase">folia-smp (P-Cores)</h3>
        <p class="text-xl font-bold mt-1 text-emerald-400">20.0 TPS</p>
        <p class="text-xs text-slate-400 mt-2">RAM: 9.4 / 12 GB</p>
      </div>
      <div class="bg-slate-800 p-4 rounded border border-slate-700">
        <h3 class="text-xs font-semibold text-slate-400 uppercase">folia-nether (E-Cores)</h3>
        <p class="text-xl font-bold mt-1 text-emerald-400">20.0 TPS</p>
        <p class="text-xs text-slate-400 mt-2">RAM: 3.1 / 6 GB</p>
      </div>
      <div class="bg-slate-800 p-4 rounded border border-slate-700">
        <h3 class="text-xs font-semibold text-slate-400 uppercase">edge-proxy</h3>
        <p class="text-xl font-bold mt-1 text-sky-400">248 Online</p>
        <p class="text-xs text-slate-400 mt-2">RAM: 0.8 / 2 GB</p>
      </div>
      <div class="bg-slate-800 p-4 rounded border border-slate-700">
        <h3 class="text-xs font-semibold text-slate-400 uppercase">folia-staging</h3>
        <p class="text-xl font-bold mt-1 text-amber-400">Ready</p>
        <p class="text-xs text-slate-400 mt-2">RAM: 2.1 / 6 GB</p>
      </div>
    </section>

    <section class="bg-slate-800 p-6 rounded border border-slate-700 space-y-4">
      <h2 class="text-lg font-bold text-slate-200">🧪 Plugin Staging & Release Pipeline</h2>
      <div class="flex gap-4">
        <button onclick="spinUpStaging()" class="bg-indigo-600 hover:bg-indigo-500 px-4 py-2 rounded text-sm font-semibold">⚡ Spin Up Staging Clone</button>
        <button onclick="destroyStaging()" class="bg-rose-700 hover:bg-rose-600 px-4 py-2 rounded text-sm font-semibold">✕ Destroy Staging</button>
      </div>

      <div class="border-2 border-dashed border-slate-600 rounded p-6 text-center">
        <input type="file" id="pluginFile" class="hidden" onchange="uploadPlugin(this.files[0])">
        <label for="pluginFile" class="cursor-pointer text-sm text-slate-300 hover:text-emerald-400">
          📁 Click to upload a Folia-compatible plugin (.jar) to Staging
        </label>
        <div id="uploadStatus" class="text-xs text-emerald-400 mt-2"></div>
      </div>

      <div class="flex justify-between items-center bg-slate-900 p-4 rounded border border-slate-700">
        <div>
          <p class="text-sm font-semibold" id="stagedPluginName">No plugin staged</p>
          <p class="text-xs text-slate-400">Validate gameplay on <code class="text-slate-300">staging.domain.com</code></p>
        </div>
        <button id="promoteBtn" disabled onclick="promoteProduction()" class="bg-emerald-600 disabled:opacity-50 hover:bg-emerald-500 px-4 py-2 rounded text-sm font-semibold">🚀 Promote to Production</button>
      </div>
    </section>
  </div>

  <script>
    let currentPlugin = "";
    async function spinUpStaging() {
      await fetch('/api/staging/spin-up', { method: 'POST' });
      alert('Staging clone created and online!');
    }
    async function destroyStaging() {
      await fetch('/api/staging/destroy', { method: 'DELETE' });
      alert('Staging environment destroyed.');
    }
    async function uploadPlugin(file) {
      const formData = new FormData();
      formData.append('file', file);
      const res = await fetch('/api/staging/upload-plugin', { method: 'POST', body: formData });
      const data = await res.json();
      if (res.ok) {
        currentPlugin = data.filename;
        document.getElementById('uploadStatus').innerText = `Uploaded: ${data.filename}`;
        document.getElementById('stagedPluginName').innerText = `Staged: ${data.filename}`;
        document.getElementById('promoteBtn').disabled = false;
      } else {
        alert(data.detail);
      }
    }
    async function promoteProduction() {
      if (!currentPlugin) return;
      const res = await fetch(`/api/production/promote/${currentPlugin}`, { method: 'POST' });
      const data = await res.json();
      alert(data.message);
    }
  </script>
</body>
</html>

```

---

## 8. Sample MythicMobs & ItemsAdder Configurations

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

## 9. Future Expansion: Public Community & Analytics Portal

An asynchronous telemetry portal that offloads player activity dashboards from active game threads:

```
       ┌──────────────────┐       ┌──────────────────┐
       │   edge-proxy     │       │    folia-smp     │
       │ (Velocity Redis) │       │ (HuskSync MySQL) │
       └────────┬─────────┘       └────────┬─────────┘
                │                          │
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
       │ LXD Container: public-web-portal            │
       │ • Next.js / Astro Static-Edge Front End     │
       │ • Public REST & WebSocket Live APIs         │
       │ • Crafatar / Minotar 3D Avatar Rendering    │
       └─────────────────────────────────────────────┘

```

### Core Features

1. **Live Network Pulse:** 3D avatar gallery of currently active players, online counts, current server location, and regional tick status.
2. **Historical Registry:** Searchable directory by username or UUID, lifetime playtime counters across Overworld vs. Nether vs. End, and join history.
3. **Leaderboard Tracking:** Top `AuraSkills` power levels, richest merchant rankings from `AxAuctions`/`Vault-Unlocked`, and blocks mined.
4. **Player Profile Cards (`/player/[uuid]`):** 3D skin renders, GitHub-style 365-day playtime heatmaps, and public settlement badges from `HuskClaims`.

---

## 10. Step-by-Step Rollout & Deployment Sequence

### Phase 1: Host Storage & Networking

1. Deploy **Ubuntu Server 24.04 LTS** onto the host.
2. Initialize ZFS on the primary NVMe:
```bash
zpool create -f default /dev/nvme0n1
zfs set compression=zstd default

```


3. Initialize LXD with ZFS backing and `lxdbr0` network bridge.

### Phase 2: Snap Build Pipeline

1. Build `folia-server` and `velocity-proxy` snaps using `snapcraft --use-lxd`.
2. Push generated `.snap` packages to host local storage `/opt/snaps/`.

### Phase 3: Container Provisioning

1. Apply LXD profiles (`folia-base`, `folia-smp-main`, `folia-subworld`, `proxy-edge`).
2. Launch core containers:
```bash
lxc launch ubuntu:24.04 edge-proxy -p folia-base -p proxy-edge
lxc launch ubuntu:24.04 folia-smp -p folia-base -p folia-smp-main
lxc launch ubuntu:24.04 folia-nether -p folia-base -p folia-subworld
lxc launch ubuntu:24.04 hub-lobby -p folia-base -p folia-subworld

```


3. Install snaps into target containers and pre-generate the Overworld with `chunky start world 10000`.

### Phase 4: Control Plane Deployment

1. Launch `mgmt-control` container with LXD socket mount:
```bash
lxc launch ubuntu:24.04 mgmt-control -p folia-base
lxc config device add mgmt-control lxd-socket disk source=/var/snap/lxd/common/lxd/unix.socket path=/var/snap/lxd/common/lxd/unix.socket

```


2. Start the FastAPI backend and serve the Web UI dashboard on internal management port `8080`.
3. Forward WAN traffic on TCP port `25565` to the `edge-proxy` container IP.

```

```
