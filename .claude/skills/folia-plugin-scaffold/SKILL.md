---
name: folia-plugin-scaffold
description: Scaffold a new in-house Folia/Paper plugin for the FoliaNexa cluster (folia-server repo), either from a feature description or by pointing at an existing mod/plugin on Modrinth and producing an original, Folia-native plugin that replicates its player-facing behavior. Use when a developer asks to create/write/build/port a plugin for this cluster, or says something like "make a plugin like <modrinth URL>" / "port this mod to Folia". Covers requirement gathering, the Modrinth lookup + feature-triage procedure (and its legal/ethical boundaries — never copy source, always disclose inspiration), the Gradle/Java 21/paper-api project skeleton, Folia-safe RegionScheduler/GlobalRegionScheduler/AsyncScheduler usage, and wiring the finished plugin toward mgmt/src/folia_mgmt/catalog.yaml per docs/plugin-dev/03-submitting-for-review.md.
---

# Folia plugin scaffold

Produces a real, buildable Folia/Paper plugin project — either from a
plain-language feature description, or from a Modrinth mod/plugin the
developer wants equivalent functionality for. This skill *does* the
scaffolding and writes real code; it isn't just pointers to the docs.
For the narrative "why" behind every decision below, the three-part
series this skill operationalizes is `docs/plugin-dev/01-environment-
setup.md`, `02-plugin-architecture.md`, and `03-submitting-for-
review.md` in this repo — read them once if anything here is unclear,
but don't make the developer read them before you can act.

## 0. Gather requirements first

Don't start writing files until you know:

1. **What the plugin does.** Either a plain description from the
   developer, or a Modrinth URL/slug to derive it from (§1).
2. **A plugin name and id.** The id is what ends up in `catalog.yaml`
   and in `--plugin <id>` — pick something that won't collide with an
   existing catalog entry (grep `mgmt/src/folia_mgmt/catalog.yaml` in
   this repo, or `folia-nexa-mgmt plugins list` against a running
   mgmt). PascalCase, matching existing entries (`LuckPerms`,
   `ServerSelector`).
3. **Where the new repo goes.** Its own git repository, sibling to
   `folia-server`, **not** nested inside it — ask the developer for a
   path (e.g. `~/src/github/<their-username>/<repo-name>`) if they
   haven't given one. Nesting it inside `folia-server` is wrong: the
   catalog only ever links to an external `download_url`, it never
   hosts plugin source (`docs/plugin-dev/03-submitting-for-review.md`
   §3.1).
4. **Target Folia/Paper version.** Default to `1.21.4` — it's this
   cluster's default engine version
   (`mgmt/src/folia_mgmt/models.py`'s `World.version`) — but ask if the
   developer's target world runs something else, and use that
   version's real `paper-api` coordinate (§3 shows how to confirm one
   exists before committing to it).
5. **A category** for the eventual catalog entry — check existing
   categories in `catalog.yaml` first (`permissions`, `economy`,
   `minigame`, `lobby`, `in-house`, etc.) and reuse one if it fits,
   rather than inventing a near-duplicate.

If the developer gave you a Modrinth link, do §1 before asking
1–5 above — the mod's own description usually answers most of them.

## 1. If porting from a Modrinth mod/plugin

### 1.1 Fetch the real project data

```bash
curl -s "https://api.modrinth.com/v2/project/<slug-or-id>"
curl -s "https://api.modrinth.com/v2/project/<slug-or-id>/version"
```

From the first call, note: `title`, `description`, `body` (the long
description — this is where the actual feature list lives),
`categories`, `license.id`, `client_side`, `server_side`. From the
second, note each version's `loaders` array.

### 1.2 Triage what kind of project this actually is — this changes everything

- **`loaders` includes `paper`/`spigot`/`bukkit`/`purpur`:** this is
  already a Bukkit-ecosystem plugin, not a mod needing a port. Say so
  to the developer and ask whether they actually want (a) a from-
  scratch reimplementation anyway (e.g. because the original uses the
  legacy Bukkit scheduler and isn't Folia-safe — a genuine reason to
  rewrite), or (b) to just add the existing plugin to the catalog
  directly, which is a completely different, much simpler flow (`docs/
  plugin-dev/03-submitting-for-review.md`, using the original's own
  release as `download_url` — no new code at all). Don't default to
  reimplementing something that already works and is already a plugin.
- **`loaders` is only `fabric`/`forge`/`neoforge`/`quilt`:** this is a
  true mod, built against a completely different API (Mixin/event bus,
  not Bukkit). There is no mechanical "port" — you're writing an
  original Paper/Folia plugin that replicates the *described
  player-facing behavior*, using entirely different (Bukkit/paper-api)
  APIs underneath. Say this explicitly to the developer so they don't
  expect a 1:1 translation.
- **`client_side: required`, `server_side: unsupported` (or the mod's
  description is clearly about client rendering/keybinds/HUD):** this
  functionality **cannot** be replicated server-side at all. A Paper
  plugin runs on the server; it can't add client-side rendering or
  keybinds without a companion client mod, which is out of scope for
  this cluster's plugin catalog. Tell the developer plainly which
  described features (if any) fall into this category and can't be
  ported, rather than silently dropping them or attempting something
  that can't work.

### 1.3 Legal/ethical boundary — do not skip this

- Read **only** the project's public page text (`description`, `body`)
  to understand *what it does*. Do **not** download the mod's actual
  jar or source and decompile/copy it, even if it's technically
  reachable — write original implementation code based on the
  described behavior, not derived from their code. This holds
  regardless of the original's license.
- If the original is itself open source under a permissive license and
  the developer wants to consult its actual source for a specific
  algorithm, that's a call for the developer to make explicitly — don't
  do it unprompted, and even then, write your own implementation rather
  than transcribing theirs.
- The finished plugin's `README.md` and catalog `notes` must disclose
  the inspiration and disclaim affiliation — the `__INSPIRATION_NOTE__`
  slot in `templates/README.md` is for exactly this. Something like:
  > Independent reimplementation inspired by [Original Name](modrinth
  > URL) for Folia compatibility. Not affiliated with or endorsed by
  > the original author. No code, assets, or text from the original
  > project were used — behavior was reimplemented from its public
  > feature description only.
- Don't name the new plugin identically to the original in a way that
  implies affiliation or that it *is* the original (e.g. "OriginalMod
  Folia Port" overclaims — "MyServerName's [feature] Plugin" or a
  genuinely distinct name is safer, and is also just accurate: it's a
  different implementation).

### 1.4 Confirm your feature summary before writing code

Restate the feature list you derived from the mod's description back
to the developer in your own words, flagging anything from §1.2/§1.3
that can't be replicated, before scaffolding. This catches
misunderstandings while they're cheap to fix.

## 2. Scaffold the project

Copy every file from this skill's `templates/` directory into the
target repo, substituting these tokens (exact string replace, every
occurrence):

| Token | Example |
| --- | --- |
| `__PLUGIN_NAME__` | `AuctionHouse` |
| `__PLUGIN_ID__` | `AuctionHouse` (same as name, used as catalog `id`) |
| `__PLUGIN_ID_LOWER__` | `auctionhouse` (used in permission nodes) |
| `__ARTIFACT_ID__` | `auction-house` (kebab-case, used as the Gradle project name / jar basename) |
| `__GROUP__` | `com.example.auctionhouse` — the developer's own reverse-domain prefix, not `com.example` literally |
| `__PACKAGE__` | same as `__GROUP__`, used as the Java package |
| `__VERSION__` | `0.1.0` (semver — `docs/plugin-dev/02-plugin-architecture.md` §2.8) |
| `__MAIN_CLASS__` | fully-qualified, e.g. `com.example.auctionhouse.AuctionHousePlugin` |
| `__MAIN_CLASS_SIMPLE__` | just the class name, e.g. `AuctionHousePlugin` |
| `__LISTENER_CLASS__` | e.g. `AuctionHouseListener` |
| `__COMMAND_CLASS__` | e.g. `AuctionHouseCommand` |
| `__COMMAND_NAME__` | e.g. `auctionhouse` — the `plugin.yml` command name |
| `__COMMAND_DESCRIPTION__` | one line |
| `__PAPER_API_VERSION__` | `1.21.4-R0.1-SNAPSHOT` for the 1.21.4 default (see §3 to confirm a different version's coordinate) |
| `__API_VERSION_MAJOR_MINOR__` | `1.21` (the `api-version:` field — major.minor only) |
| `__ONE_LINE_DESCRIPTION__` | what the plugin does, one sentence |
| `__INSPIRATION_NOTE__` | the disclosure from §1.3, or delete the line entirely for a from-scratch plugin |
| `__LICENSE__` | whatever the developer wants (their choice — this skill doesn't pick one for them) |

Files: `settings.gradle.kts`, `build.gradle.kts`, `gitignore` (rename to
`.gitignore` in the target repo), `plugin.yml` and `config.yml` (go
under `src/main/resources/`), `MainClass.java`, `ExampleListener.java`,
`ExampleCommand.java` (go under `src/main/java/<package-path>/`, and
should be renamed from `MainClass.java` to `__MAIN_CLASS_SIMPLE__.java`
etc. to match their public class name — Java requires this), `README.md`.

Then:

```bash
cd <target-repo>
git init   # if not already
```

## 3. Confirm the paper-api coordinate actually exists (if not using 1.21.4)

Don't guess a version string — check it resolves before writing it into
`build.gradle.kts`:

```bash
curl -s "https://repo.papermc.io/repository/maven-public/io/papermc/paper/paper-api/" | grep -oE "<VERSION>-R0\.1-SNAPSHOT"
```

If that prints nothing, that exact version isn't published — use the
closest one that is (the same page listing shows what's available).

## 4. Write the actual plugin logic

This is the part templates can't do for you — the example
listener/command are illustrative skeletons, not the real feature.
Replace their `TODO` bodies (and add new classes) with whatever §0/§1
established the plugin should do, following these rules throughout
(full explanation: `docs/plugin-dev/02-plugin-architecture.md`):

- **Never use `getServer().getScheduler().runTask*`** for anything
  touching a `World`/`Block`/`Entity`/`Player`. Use, verified real APIs
  on `Bukkit`:
  - `Bukkit.getRegionScheduler()` — location-tied work (`run`,
    `runDelayed`, `runAtFixedRate`, each taking a `World`+chunk-coords
    or a `Location` overload, plus a `Consumer<ScheduledTask>`).
  - `Bukkit.getGlobalRegionScheduler()` — non-location-tied plugin-wide
    work. Same method shapes, no location parameter.
  - `Bukkit.getAsyncScheduler()` — I/O or CPU work touching no Bukkit
    API (HTTP, file, JSON). Takes a real `TimeUnit`, not ticks. Hop
    back onto `RegionScheduler` from inside the callback if the result
    needs to touch game state.
  - `Bukkit.isOwnedByCurrentRegion(entity|location|block)` to check
    defensively when unsure which thread you're on.
- **Keep domain logic in plain Java classes** (no `org.bukkit.*`
  imports) called from thin listener/command adapters — this is what
  makes the logic unit-testable with plain JUnit and keeps the
  Bukkit-touching surface small enough to audit for the rule above.
  Add real tests (`src/test/java/...`) for that logic as you write it,
  not as an afterthought.
- **Config**: `saveDefaultConfig()` in `onEnable`, a `reload` subcommand
  calling `reloadConfig()` — already wired in the templates.
- **Permissions**: every node gets an explicit `default:` in
  `plugin.yml` — never leave one unset (implicitly `op`-only, easy to
  get surprised by later).

## 5. Build and verify

```bash
cd <target-repo>
# generate the wrapper once, if it doesn't exist yet (needs a JDK 21 on
# PATH — docs/plugin-dev/01-environment-setup.md §1.1/§1.4 — and a
# temporary Gradle just for this one command):
gradle wrapper --gradle-version 8.10
./gradlew build
```

If this environment has no JDK/Gradle available to actually run that
(check `which java` / `which gradle` first), say so plainly and give
the developer the exact commands to run themselves rather than
claiming success you didn't verify — this project's own convention
(see `CLAUDE.md`'s "what's real vs. documented-but-unverified") is to
never claim something works without having actually run it.

## 6. Self-review before calling it done

Straight from `docs/plugin-dev/03-submitting-for-review.md` §3.4 —
worth restating here since this is the point you'd otherwise stop:

- No `getServer().getScheduler().runTask*` hits touching game state
  (`grep -rn "getScheduler()\.\(runTask\|runTaskLater\|runTaskTimer\|runTaskAsynchronously\)" src/`
  — read the matches, a zero count alone isn't proof of nothing).
- Actually built successfully (§5), and — if there's any way to run a
  local Folia test server in this environment or the developer can —
  actually loaded and smoke-tested, not just compiled.
- `plugin.yml`'s `api-version` matches the `paper-api` coordinate used.
- Every permission has an explicit `default:`.
- If Modrinth-derived: the inspiration/non-affiliation disclosure from
  §1.3 is actually present in `README.md`.

## 7. Point the developer at the next step

Scaffolding a working plugin isn't the same as it being available on
the cluster — that's a separate, deliberate step
(`docs/plugin-dev/03-submitting-for-review.md`, and note its §3.6: a
merged catalog PR alone doesn't ship to a running mgmt install either).
Tell the developer, don't just silently stop:

1. Cut a release (tag + build + publish the jar somewhere with a stable
   URL — GitHub Releases is simplest) once the plugin actually does
   what it's meant to.
2. `sha256sum` the published artifact.
3. Fill in `templates/catalog-entry.yaml`'s tokens
   (`__DOWNLOAD_URL__`/`__SHA256__` need the real release from step 1
   and 2; leave them as literal `null` / set `__VERIFIED__: false` if
   you're drafting the entry before that release exists) and open a PR
   against `folia-server` adding it to
   `mgmt/src/folia_mgmt/catalog.yaml`.

Offer to do 3 for them once 1–2 are done — don't do it preemptively
with placeholder values presented as if they were real.
