# 1. Setting Up Your Development Environment (Ubuntu)

Part of the [FoliaNexa plugin development series](README.md). This part
gets a brand-new Ubuntu machine to the point where you can build a
plugin jar and load it into a real Folia server running on your own
laptop — no cluster, no mgmt, no proxy required for any of this. Parts
[2](02-plugin-architecture.md) and [3](03-submitting-for-review.md)
cover writing the plugin well and getting it into the cluster's catalog.

**Assumed background:** you can write and read Java. You do **not**
need to have written a Minecraft server plugin before, or know anything
about Bukkit/Paper/Folia's APIs — that starts in part 2. Commands below
assume Ubuntu 24.04+ (anything `apt`-based and recent enough to have
`openjdk-21-jdk` in its repos works the same way).

## 1.1 Install a JDK

Paper and Folia plugins are compiled against Java 21 — that's the
language level the whole FoliaNexa project standardizes on (see
`proxy/build.gradle.kts`'s toolchain, and `CLAUDE.md`'s test-running
instructions). Install it from Ubuntu's own repos:

```bash
sudo apt update
sudo apt install openjdk-21-jdk
java -version   # should print openjdk version "21...."
javac -version  # should print javac 21....
```

If `java -version` prints a different major version and you have
multiple JDKs installed, `sudo update-alternatives --config java` (and
`--config javac`) lets you pick which one is default. Gradle (next
section) can also be pointed at a specific JDK independent of your
shell's default via `JAVA_HOME`, if you'd rather not touch
system-wide alternatives.

## 1.2 Install git

```bash
sudo apt install git
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

You'll need this both for your plugin's own repository (part 3 covers
why an in-house plugin lives in its own repo, not inside
`folia-server`) and to clone `folia-server` itself if you want to read
the catalog or existing plugin entries locally.

## 1.3 Pick an IDE

Either works well; pick based on preference.

**IntelliJ IDEA Community (recommended)** — the most common choice for
Gradle/Java plugin development, with built-in Gradle sync, run
configurations, and a debugger that attaches to a running JVM (useful
for stepping through plugin code while a test server is live):

```bash
sudo snap install intellij-idea-community --classic
```

Open your plugin's project directory (once it has a `build.gradle.kts`
— see §1.5) and IntelliJ auto-detects it as a Gradle project and
resolves dependencies on first open. That first resolve downloads the
Paper API jar and its transitive dependencies; it can take a minute.

**VS Code**, if you'd rather stay lightweight:

```bash
sudo snap install code --classic
```

then install the "Extension Pack for Java" and "Gradle for Java"
extensions from within VS Code (`Ctrl+Shift+X`, search, install). Less
integrated than IntelliJ for Gradle-specific workflows, but perfectly
usable.

## 1.4 Gradle: use the wrapper, not a system install

Every Java component in this repo (`proxy/`) builds with Gradle's
**wrapper** (`./gradlew`) rather than a system-installed `gradle` — the
wrapper pins an exact Gradle version per-project
(`gradle/wrapper/gradle-wrapper.properties`), so a build behaves the
same on your machine, a teammate's, and CI regardless of what's
installed globally. Do the same for your plugin. You don't need to
`apt install` or `snap install` Gradle at all — §1.5 shows generating
the wrapper from a temporary Gradle invocation, after which `./gradlew`
is entirely self-contained (it downloads its own pinned Gradle
distribution into `~/.gradle` on first run).

## 1.5 Scaffold a new plugin project

There's no project-generator CLI for this repo's plugins (no
`org.papermc.paperweight` archetype is set up here) — start from a
plain Gradle Java project and add the Paper dependency by hand. This
mirrors exactly how `proxy/` is set up (a real, working Gradle+JVM
plugin project you can read directly for comparison), just targeting
Paper's plugin API instead of Velocity's:

```bash
mkdir my-folianexa-plugin && cd my-folianexa-plugin
git init
```

`settings.gradle.kts`:

```kotlin
rootProject.name = "my-folianexa-plugin"
```

`build.gradle.kts` — pins Java 21 and pulls `paper-api` from PaperMC's
own Maven repo (the same repo `proxy/build.gradle.kts` already uses for
`velocity-api`; `paper-api` lives at the same host):

```kotlin
plugins {
    java
    id("com.gradleup.shadow") version "8.3.5"
}

group = "com.example.myplugin"   // use your own reverse-domain prefix
version = "0.1.0"

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(21))
    }
}

repositories {
    mavenCentral()
    maven("https://repo.papermc.io/repository/maven-public/") // hosts io.papermc.paper:paper-api
}

dependencies {
    // 1.21.4 matches this cluster's default engine version
    // (mgmt/src/folia_mgmt/models.py's World.version) — match whatever
    // version the world you're targeting actually runs.
    compileOnly("io.papermc.paper:paper-api:1.21.4-R0.1-SNAPSHOT")

    testImplementation(platform("org.junit:junit-bom:5.10.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
}

tasks.test {
    useJUnitPlatform()
}

tasks.shadowJar {
    archiveClassifier.set("") // the fat jar IS the plugin jar
}

tasks.build {
    dependsOn(tasks.shadowJar)
}
```

`paper-api` is `compileOnly` deliberately: the real implementation is
already on the server's classpath at runtime (it *is* the server), so
bundling it into your plugin jar would just bloat it and risk
classloader conflicts. The Shadow plugin exists for the dependencies
you *do* need bundled — anything beyond `paper-api` itself (an HTTP
client, a JSON library, etc.) — same reasoning `proxy/` documents in its
own `build.gradle.kts` comments.

Generate the wrapper (one-time; needs a JDK on `PATH`, which §1.1 set
up, and a temporary Gradle — the snap Gradle mentioned in §1.4's
alternative works fine just for this one command, or use
[`sdkman`](https://sdkman.io/) if you already have it):

```bash
sudo snap install gradle --classic   # temporary, only to generate the wrapper
gradle wrapper --gradle-version 8.10 # matches proxy/'s pinned version
sudo snap remove gradle              # not needed again — ./gradlew handles everything from here
```

Project layout at this point:

```
my-folianexa-plugin/
├── build.gradle.kts
├── settings.gradle.kts
├── gradlew
├── gradlew.bat
├── gradle/wrapper/
└── src/
    ├── main/java/...        (part 2 covers what goes here)
    └── main/resources/
        └── plugin.yml       (part 2 covers this too)
```

Confirm the build works even with no plugin code yet:

```bash
./gradlew build
```

## 1.6 Run a local Folia server to test against

You don't need the FoliaNexa cluster, mgmt, or a proxy to iterate on a
plugin — a single Folia server running directly on your dev machine is
faster to restart and easier to attach a debugger to. **§1.8 below
(`tools/folia-nexa-spawn.sh`) automates everything in this section and
§1.7 into one command, and is the recommended way to do this day to
day** — read §1.6/§1.7 once anyway, since they're what the tool is
actually doing and knowing that matters for debugging it. Pick a
scratch directory outside your plugin's own repo:

```bash
mkdir -p ~/folia-test-server && cd ~/folia-test-server
```

Folia doesn't publish fixed download URLs per version — like the rest
of this project (`CLAUDE.md`'s note about `fill.papermc.io/v3`), fetch
the latest build for the version you're targeting from PaperMC's real
API:

```bash
VERSION=1.21.4
BUILD_JSON=$(curl -s "https://fill.papermc.io/v3/projects/folia/versions/$VERSION/builds")
DOWNLOAD_URL=$(echo "$BUILD_JSON" | python3 -c "import json,sys; b=json.load(sys.stdin)[0]; print(b['downloads']['server:default']['url'])")
curl -L -o folia-server.jar "$DOWNLOAD_URL"
```

(`[0]` picks the latest build — the API returns builds newest-first.)

First run generates the EULA file and exits — accept it, then start
the server for real:

```bash
java -Xms2G -Xmx2G -jar folia-server.jar --nogui
echo "eula=true" > eula.txt
java -Xms2G -Xmx2G -jar folia-server.jar --nogui
```

Useful `server.properties` tweaks for a local dev loop (edit before the
second start, or stop the server and edit, then restart):

- `online-mode=false` — lets you connect without a real Mojang account
  check, handy on a LAN-only test box. **Never** run a real,
  internet-reachable server this way — anyone can connect as any
  username. Fine for `localhost`-only testing.
- `difficulty=peaceful` and `spawn-protection=0` — reduces noise while
  you're focused on plugin logic, not survival gameplay.

Connect from your regular Minecraft client to `localhost:25565` with a
matching game version. Folia's whole point is regionized multithreaded
world simulation — that only actually exercises anything interesting
once there's more than one player/entity cluster active in different
areas, so for testing region-boundary behavior specifically, consider
flying to a spot 500+ blocks from spawn and testing there too, not just
next to your single spawn point.

## 1.7 The iteration loop

```bash
./gradlew build
cp build/libs/my-folianexa-plugin-0.1.0.jar ~/folia-test-server/plugins/
```

then in the server console (or in-game as an op, if the plugin
supports it): `/reload confirm` reloads plugins without a full restart,
but **Paper's own docs discourage `/reload`** for anything beyond
trivial changes — it doesn't fully re-initialize a lot of internal
state and can leave a server in a subtly broken condition. For real
iteration, stop the server (`/stop` in console) and start it again. A
small script saves the retyping:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd ~/my-folianexa-plugin && ./gradlew build
cp build/libs/*.jar ~/folia-test-server/plugins/
cd ~/folia-test-server && java -Xms2G -Xmx2G -jar folia-server.jar --nogui
```

## 1.8 `folia-nexa-spawn.sh`: the recommended day-to-day workflow

§1.6-1.7 is the loop worth understanding once by hand, but you don't
need to keep re-typing it. **This is the recommended way to run and
iterate on a plugin locally** —
[`tools/folia-nexa-spawn.sh`](../../tools/folia-nexa-spawn.sh) in the
`folia-server` repo does the whole thing in one command: fetches (and
caches) the right Folia build, builds your plugin from local source
with `./gradlew build`, drops the resulting jar into a scratch server's
`plugins/`, starts the server, and prints the address to connect a
client to once it's actually up. Reach for §1.6/§1.7's manual steps
only if you need something the script doesn't do (e.g. a from-source
custom Folia build) — otherwise, use this.

**Prerequisites beyond §1.1's JDK** — `curl` and `python3` (used to
call `fill.papermc.io` and parse its JSON, and to pick a free local
port). Both ship by default on Ubuntu 24.04 desktop and most server
images, but on a minimal/server install confirm with:

```bash
curl --version && python3 --version
# if missing:
sudo apt install curl python3
```

No system Gradle needed — same as §1.4, your plugin's own `./gradlew`
wrapper handles that.

From the `folia-server` repo checkout (a sibling clone next to your
plugin's own repo — see [part 3](03-submitting-for-review.md) for why
plugin source lives in its own repo, not inside `folia-server`):

```bash
tools/folia-nexa-spawn.sh 1.21.4 overworld --plugindir=~/my-folianexa-plugin
```

`1.21.4` is the Folia version to run (same version string §1.6's manual
`VERSION=` uses); `overworld` is a label for this test world — it also
picks `level-type` (`lobby`/`minigame`/`staging` get a flat world, since
those are normally hand-built rather than generated; anything else gets
normal terrain). Pass `--plugindir` again to load more than one plugin
at once (handy for a plugin that depends on `LuckPerms` or another
catalog entry — point it at a local checkout of that too, or just drop
a prebuilt jar into the server's `plugins/` directory directly, which
lives under `~/.local/share/folia-nexa-spawn/<version>-<world-type>/`
by default).

The server runs in the foreground with the console attached, exactly
like running `java -jar ... --nogui` by hand — type commands directly,
`Ctrl+C` or `/stop` shuts it down. Once it's actually up, you'll see:

```
==============================================================
 Folia 1.21.4 (overworld) is up.
 Connect to your world with localhost:53214
 (offline-mode — any username works)
 World save + logs: /home/you/.local/share/folia-nexa-spawn/1.21.4-overworld
 Stop the server with /stop in this console, or Ctrl+C.
==============================================================
```

The port is auto-picked free each run (so `overworld` and a second
`lobby` instance can run side by side); pass `--port` to pin one. The
world save persists across runs in that same directory — re-run the
same command to relaunch with your existing world and just the plugin
rebuilt; add `--clean` for a fresh world, or `--no-build` to skip
`./gradlew build` and relaunch faster when only server-side config
changed. `tools/folia-nexa-spawn.sh --help` covers the rest (`--memory`,
`--workdir`, `--online-mode`, `--refresh-jar`).

## Next

[Part 2: writing a sound, well-architected plugin](02-plugin-architecture.md) —
project structure, `plugin.yml`, and (the part that's actually specific
to Folia rather than any Paper plugin) the region-scheduler APIs you
must use instead of the legacy Bukkit scheduler.
