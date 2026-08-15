# 2. Writing a Sound, Well-Architected Plugin

Part of the [FoliaNexa plugin development series](README.md). Assumes
you've completed [part 1](01-environment-setup.md) — a working Gradle
project with `paper-api` on the classpath and a local Folia server to
test against. This part covers project structure and, specifically,
the thing that makes a *Folia* plugin different from an ordinary Paper
plugin: you cannot use the scheduler patterns most Bukkit/Paper
tutorials on the internet teach. Get this part wrong and your plugin
will appear to work fine on your single-player-feeling local test
server and then throw `IllegalStateException`s or corrupt state under
real concurrent load — exactly the failure mode this doc exists to
prevent before it reaches the catalog (part 3's review checks for it).

## 2.1 `plugin.yml`

Paper (and Folia, which is a Paper fork) reads plugin metadata from
`src/main/resources/plugin.yml`. A real, minimal example — this is the
actual shape used by the `ServerSelector` catalog entry
(`mgmt/src/folia_mgmt/catalog.yaml`), reproduced here as a working
reference rather than an invented one:

```yaml
name: MyFoliaNexaPlugin
version: '0.1.0'
main: com.example.myplugin.MyFoliaNexaPlugin
api-version: '1.21'
load: POSTWORLD

commands:
  myplugin:
    description: Does the thing
    usage: /myplugin [reload]

permissions:
  myplugin.reload:
    description: Reload the config
    default: op
```

- `main` is the fully-qualified class name of your plugin's entry
  point (§2.2) — package it under your own reverse-domain prefix, not
  `com.example`.
- `api-version` should match the Paper API major/minor you compiled
  against (`'1.21'` for the `paper-api:1.21.4-R0.1-SNAPSHOT` dependency
  from part 1) — this is what lets Paper warn loudly if your plugin is
  loaded on a server too old for the API surface you're using, instead
  of failing confusingly at runtime.
- `load: POSTWORLD` (the default if omitted) is almost always what you
  want — your plugin loads after worlds exist. `STARTUP` is for the
  rare plugin that needs to hook world-generation itself before any
  world loads.

There's also a newer, alternative Paper-specific `paper-plugin.yml`
format (bootstrap-based, supports dependency resolution at load time).
It has real advantages for complex plugins but also real rough edges
and less universal tooling support as of Paper 1.21.x. Stick with the
classic `plugin.yml` above unless you have a specific reason not to —
every catalog entry in this project uses it, and it's the format assumed
by the rest of this doc.

## 2.2 The main class

```java
package com.example.myplugin;

import org.bukkit.plugin.java.JavaPlugin;

public final class MyFoliaNexaPlugin extends JavaPlugin {

    @Override
    public void onEnable() {
        saveDefaultConfig();
        getServer().getPluginManager().registerEvents(new MyListener(this), this);
        getCommand("myplugin").setExecutor(new MyCommand(this));
    }

    @Override
    public void onDisable() {
        // Cancel any of your own outstanding scheduled tasks here (§2.3) —
        // Paper cancels tasks scheduled through *its* schedulers
        // automatically on plugin disable, but anything you're tracking
        // yourself (e.g. a raw ExecutorService) is your responsibility.
    }
}
```

Keep `onEnable`/`onDisable` thin — wiring, not logic. The actual
behavior belongs in separate, independently testable classes (§2.6).

## 2.3 Scheduling: the part that's actually different about Folia

This is the one section in this whole series you cannot skip or skim.

**Why it matters:** a normal Paper server ticks the entire world on one
thread. Folia doesn't — it splits the world into independent regions,
each ticked on its own thread, running concurrently. `BukkitScheduler`
(`getServer().getScheduler().runTaskLater(...)`, `runTaskTimer(...)`,
etc. — what every pre-Folia Bukkit/Spigot/Paper tutorial teaches) has
**no defined thread** to run your task on in that model, because there
is no longer a single "the main thread." Calling it on Folia either
throws immediately or — worse — silently runs your callback on the
wrong thread, touching entity/world/block state a different region's
thread also owns concurrently, corrupting state in ways that only show
up under real load with multiple regions active (exactly the kind of
bug that survives a single-player local test and then breaks in
production, PLAN.md §14's whole reason for the Folia-compatibility
caveats on classic minigame plugins).

Folia's replacement API lives in
`io.papermc.paper.threadedregions.scheduler` and is part of `paper-api`
itself — no extra dependency, verified present in the real
`paper-api-1.21.4` jar. Three schedulers, each for a different kind of
work, all reached off `Bukkit`:

**`RegionScheduler`** — `Bukkit.getRegionScheduler()` — for anything
tied to a specific location (a block, an entity's position, a chunk).
Runs on the thread that owns *that* region:

```java
public interface RegionScheduler {
    void execute(Plugin plugin, World world, int chunkX, int chunkZ, Runnable task);
    ScheduledTask run(Plugin plugin, World world, int chunkX, int chunkZ,
                       Consumer<ScheduledTask> task);
    ScheduledTask runDelayed(Plugin plugin, World world, int chunkX, int chunkZ,
                              Consumer<ScheduledTask> task, long delayTicks);
    ScheduledTask runAtFixedRate(Plugin plugin, World world, int chunkX, int chunkZ,
                                  Consumer<ScheduledTask> task, long initialDelayTicks, long periodTicks);
    // each also has a Location-based overload — same behavior, resolves
    // chunkX/chunkZ from the Location for you
}
```

Example — spawn a firework at a player's location three seconds later,
correctly (works no matter which region the player has moved to by
then, because the task is scheduled against the *player's current
location at schedule time* — re-check the player's position inside the
callback if they might have moved regions in the meantime):

```java
Bukkit.getRegionScheduler().runDelayed(plugin, player.getLocation(), task -> {
    if (player.isOnline()) {
        player.getWorld().spawnEntity(player.getLocation(), EntityType.FIREWORK_ROCKET);
    }
}, 60L); // ticks — 20 ticks/sec
```

**`GlobalRegionScheduler`** — `Bukkit.getGlobalRegionScheduler()` — for
work that isn't tied to any location: global plugin state, a periodic
housekeeping task, anything your `ServerSelector`-style lobby plugin
does that isn't about one specific player's position:

```java
public interface GlobalRegionScheduler {
    void execute(Plugin plugin, Runnable task);
    ScheduledTask run(Plugin plugin, Consumer<ScheduledTask> task);
    ScheduledTask runDelayed(Plugin plugin, Consumer<ScheduledTask> task, long delayTicks);
    ScheduledTask runAtFixedRate(Plugin plugin, Consumer<ScheduledTask> task,
                                  long initialDelayTicks, long periodTicks);
    void cancelTasks(Plugin plugin);
}
```

**`AsyncScheduler`** — `Bukkit.getAsyncScheduler()` — for work that
touches **no** Bukkit API at all: HTTP calls, file I/O, JSON parsing,
anything CPU-bound-but-not-game-state. Same idea as
`BukkitScheduler.runTaskAsynchronously` used to be, just under Folia's
own task-tracking so it's cancelled correctly on plugin disable. Note
the time unit is a real `TimeUnit`, not ticks:

```java
public interface AsyncScheduler {
    ScheduledTask runNow(Plugin plugin, Consumer<ScheduledTask> task);
    ScheduledTask runDelayed(Plugin plugin, Consumer<ScheduledTask> task,
                              long delay, TimeUnit unit);
    ScheduledTask runAtFixedRate(Plugin plugin, Consumer<ScheduledTask> task,
                                  long initialDelay, long period, TimeUnit unit);
    void cancelTasks(Plugin plugin);
}
```

**The rule of thumb:** if your task touches a `World`, `Block`,
`Entity`, or `Player` — use `RegionScheduler`, scheduled against that
object's location. If it's global plugin bookkeeping — use
`GlobalRegionScheduler`. If it's neither (pure computation or I/O) —
`AsyncScheduler`, and if the result needs to feed back into game state,
hop back onto the right region via `RegionScheduler` from inside the
async callback rather than touching Bukkit objects directly off-thread.

**Checking you're on the right thread**, when you're not sure (e.g.
inside a callback triggered indirectly):
`Bukkit.isOwnedByCurrentRegion(entity)` /
`Bukkit.isOwnedByCurrentRegion(location)` /
`Bukkit.isOwnedByCurrentRegion(block)` — all real, verified methods on
`Bukkit` — return whether the *currently executing thread* owns the
region containing that object. Reaching for this defensively in a spot
you're unsure about is far better than guessing and finding out in
production.

**Event listeners** already run on the correct region's thread for
whatever triggered them (Paper handles that dispatch for you) — the
scheduling APIs above matter for anything *you* schedule (delayed,
repeating, or dispatched from another thread), not for ordinary event
handlers.

## 2.4 Config

```java
@Override
public void onEnable() {
    saveDefaultConfig();   // copies config.yml from resources/ on first run, no-op after
    // ...
}

public void reload() {
    reloadConfig();
    // re-derive any cached values from getConfig() here
}
```

Ship a `src/main/resources/config.yml` with sane defaults — Paper
copies it into the plugin's data folder on first run via
`saveDefaultConfig()`. Expose a `/yourcommand reload` (matching the
`ServerSelector` example's `/selector reload`) rather than requiring a
full server restart for config changes.

## 2.5 Commands & permissions

The `commands:`/`permissions:` blocks in `plugin.yml` (§2.1) plus a
`CommandExecutor` are enough for most plugins and are what this doc
recommends by default — simple, well-documented, and what every
existing catalog entry uses:

```java
public final class MyCommand implements CommandExecutor {
    private final MyFoliaNexaPlugin plugin;

    public MyCommand(MyFoliaNexaPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (args.length > 0 && args[0].equals("reload") && sender.hasPermission("myplugin.reload")) {
            plugin.reloadConfig();
            sender.sendMessage("Reloaded.");
            return true;
        }
        sender.sendMessage("Usage: /myplugin [reload]");
        return true;
    }
}
```

If you want argument suggestions/tab-completion beyond what the legacy
`TabCompleter` interface gives you, Paper's newer Brigadier-based
command API is the alternative — registered via a lifecycle event
instead of `plugin.yml`'s `commands:` block:

```java
@Override
public void onEnable() {
    getLifecycleManager().registerEventHandler(LifecycleEvents.COMMANDS, event -> {
        event.registrar().register(
            Commands.literal("myplugin")
                .then(Commands.literal("reload")
                    .requires(src -> src.getSender().hasPermission("myplugin.reload"))
                    .executes(ctx -> {
                        reloadConfig();
                        ctx.getSource().getSender().sendMessage("Reloaded.");
                        return com.mojang.brigadier.Command.SINGLE_SUCCESS;
                    }))
                .build()
        );
    });
}
```

Either approach is fine — don't feel obligated to reach for the newer
API for a simple plugin like the example above.

Default every permission node explicitly (`default: op` or
`default: true`) rather than leaving it unset — an unset default is
`op`-only implicitly, which is easy to get surprised by later.

## 2.6 Architecture: keep game logic testable

The single most valuable structural decision you can make: **separate
your plugin's actual logic from its Bukkit/Folia API glue.** Concretely:

- Pure Java classes (no `org.bukkit.*` imports) hold your actual rules
  — a scoring algorithm, a cooldown tracker, a claim-boundary check,
  whatever your plugin's domain logic is. These are plain objects you
  can unit-test with JUnit and no server runtime at all.
- Thin adapter classes — your `Listener` implementations, command
  executors, and the scheduler callbacks from §2.3 — do nothing but
  translate a Bukkit event/command into a call on your domain logic,
  and translate the result back into Bukkit API calls (spawning an
  entity, sending a message, etc.).

This isn't just testability for its own sake — it's specifically what
makes the Folia threading rules in §2.3 tractable. A thin adapter layer
means there are only a few, easy-to-audit places where your code
touches Bukkit objects at all, so reviewing (or writing) "is this
scheduled correctly?" is a search through a handful of listener/command
classes instead of your entire codebase.

**On `MockBukkit`:** a popular library for unit-testing Bukkit plugins
against a simulated server. It's useful for testing the adapter layer's
wiring (does this command call the right thing?) but it does **not**
model Folia's region-threading behavior — a test passing under
MockBukkit is not evidence your scheduling is Folia-safe. Treat it as a
tool for testing plugin wiring and your domain logic's Bukkit-facing
edges, not as a substitute for actually running your plugin on a real
Folia server (part 1, §1.6/§1.7) before considering it done.

## 2.7 Logging

Use `getLogger()` (a `java.util.logging.Logger` Paper wires up with
your plugin's name as a prefix automatically), not `System.out.println`
— it's filterable, timestamped, and shows up correctly in the server's
own log files.

## 2.8 Versioning

Use semver (`MAJOR.MINOR.PATCH`) for your plugin's `version` in
`plugin.yml` and your release tags — this is the version string that
ends up in `catalog.yaml`'s `version` field (part 3), which the whole
point of curating in the first place is to let operators pin to a
version they've actually vetted rather than always tracking whatever's
newest.

## Next

[Part 3: submitting your plugin for review](03-submitting-for-review.md) —
publishing a release, and getting it into `catalog.yaml` so worlds can
actually declare it.
