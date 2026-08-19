# 3. Submitting Your Plugin for Review

Part of the [FoliaNexa plugin development series](README.md). Assumes
you have a working plugin built and tested per
[part 1](01-environment-setup.md) and
[part 2](02-plugin-architecture.md). This part covers getting it from
"builds on my machine" to "an operator can declare it on a world with
`--plugin your-id`."

## 3.1 Where your plugin's code lives

Your plugin gets its own git repository — **not** a directory inside
`folia-server`. The catalog (`mgmt/src/folia_mgmt/catalog.yaml`) is
just an index of `id → download_url`; it doesn't care where that URL
points, and this repo doesn't build or host plugin jars for anyone
(PLAN.md §14A). See the catalog's `ServerSelector`/`LuckPerms`/etc
entries for what a real, verified entry looks like — an `id`, a real
`download_url` and `sha256`, and `verified: true` once you've actually
downloaded and hashed the jar yourself.

## 3.2 Cut a release

Tag a version matching `plugin.yml`'s `version` (semver, per part 2
§2.8), build the shadow jar, and publish it somewhere with a stable,
directly-downloadable URL:

```bash
./gradlew build
# build/libs/my-folianexa-plugin-0.1.0.jar
```

A GitHub Release with the jar attached as a release asset is the
simplest option if your repo is on GitHub — `gh release create v0.1.0
build/libs/my-folianexa-plugin-0.1.0.jar` gives you a permanent
`github.com/<you>/<repo>/releases/download/v0.1.0/<jar>` URL. Modrinth/
Hangar work too (see the catalog's `ServerSelector` entry for what that
looks like) — anywhere that gives you a stable direct-download link is
fine.

Compute the real checksum of exactly the file you published — this is
what goes in the catalog entry's `sha256`, and what lets an operator (or
node, or you, later) verify the download hasn't been tampered with or
silently changed upstream:

```bash
sha256sum build/libs/my-folianexa-plugin-0.1.0.jar
```

## 3.3 Write the catalog entry

`PluginEntry` (`mgmt/src/folia_mgmt/plugin_catalog.py`) defines the
schema:

```yaml
- id: MyFoliaNexaPlugin
  category: your-category        # e.g. "minigame", "lobby", "utility" — freeform, but check catalog.yaml for existing categories first so `plugins list --category` stays meaningful
  source: in-house
  version: "0.1.0"
  download_url: "https://github.com/you/my-folianexa-plugin/releases/download/v0.1.0/my-folianexa-plugin-0.1.0.jar"
  sha256: "<the real sha256sum output from §3.2, lowercase hex, no filename>"
  homepage: "https://github.com/you/my-folianexa-plugin"
  verified: true
  notes: >
    Brief description of what it does and any caveats an operator
    should know before deploying it (Folia-compatibility notes,
    dependencies on other plugins like LuckPerms, anything version-
    pinned for a reason).
```

`id` is the stable key worlds reference via `--plugin <id>` — pick
something that won't collide with an existing entry
(`folia-nexa-mgmt plugins list` to check) and won't need to change
later; renaming it later is a breaking change for anyone who already
declared it on a world.

**On `verified: true`:** this project's convention (see `catalog.yaml`'s
own header comment) is that it means someone actually downloaded the
exact `download_url` and confirmed the `sha256` for real — not "this
plugin looks legitimate." Only set it once you've done that yourself
(§3.2 already did, if you followed it in order). Don't set
`verified: true` on someone else's entry you haven't personally checked.

## 3.4 Self-review checklist before opening a PR

Everything here is something you can check yourself, without waiting
on a reviewer:

- [ ] **No legacy scheduler on anything touching game state.** A quick
      smoke check (not exhaustive — read the matches, don't just trust
      a zero count):
      ```bash
      grep -rn "getScheduler()\.\(runTask\|runTaskLater\|runTaskTimer\|runTaskAsynchronously\)" src/
      ```
      Any hit needs a look: is it actually fine (rare), or does it need
      to move to `RegionScheduler`/`GlobalRegionScheduler`/
      `AsyncScheduler` per part 2 §2.3?
- [ ] **Actually run on a real local Folia server** (part 1 §1.8 —
      `tools/folia-nexa-spawn.sh`, the recommended way; §1.6/§1.7 covers
      doing it by hand), not just compiled successfully or passed
      `MockBukkit` tests (part 2 §2.6 explains why that's not
      sufficient evidence on its own).
- [ ] **`plugin.yml`'s `api-version` matches** what you compiled against.
- [ ] **Config has sane defaults** and a `reload` command if it's meant
      to be editable without a restart.
- [ ] **No hardcoded secrets, and no network calls to anything other
      than what your `notes` disclose** — a plugin that phones home
      somewhere undocumented is a real trust problem for anyone running
      it on player-facing infrastructure.
- [ ] **The release URL actually resolves and the sha256 matches** —
      `curl -sL <download_url> | sha256sum` and compare against what
      you wrote in the entry.
- [ ] **End-to-end against a real (test) mgmt instance**, if you can
      run one (`mgmt/`'s own `CLAUDE.md`-documented dev setup): drop
      your entry into a local
      `$FOLIA_MGMT_STATE_DIR/plugin-catalog-override.yaml`, declare a
      throwaway world with `--plugin <your-id>`, and confirm `GET
      /api/v1/worlds/<name>/plugins-manifest` includes it with the
      right URL. This is the same override mechanism described in part
      3's next section — a good way to dry-run your entry before it's
      merged anywhere.

## 3.5 Open the PR

A PR against `folia-server` adding your entry to
`mgmt/src/folia_mgmt/catalog.yaml` (alphabetical-by-`id` isn't
mechanically enforced but `load_catalog` sorts output that way — keep
the file itself roughly sorted too, for reviewers' sanity). Commit
message: say what the plugin does and why an operator would want it,
not just "add MyFoliaNexaPlugin to catalog" — the *why* is what a
reviewer (and a future operator skimming `git log`) actually needs.

A reviewer is going to re-check the same things §3.4 already asked you
to check yourself — the self-review checklist isn't a formality, it's
genuinely most of what review consists of here. Beyond that, review
also weighs things a first-time contributor might not think to ask:

- Does this duplicate an existing catalog entry in the same category?
  If there's already a `lobby`-category server selector, does a second
  one pull its weight, or should this be a `notes` addition to the
  existing entry instead?
- Is the plugin's own license compatible with linking to it this way?
  (The catalog links to your release, it doesn't rehost the jar — but
  if your plugin bundles/depends on something with redistribution
  restrictions, that's worth surfacing in `notes`.)
- For anything touching shared infrastructure this cluster already
  automates (LuckPerms' MySQL backend, the Discord access gate) — does
  it conflict with or duplicate that automation instead of composing
  with it?

## 3.6 What happens after merge

This is the part that's easy to get wrong expectations about: **merging
the PR alone doesn't make the plugin available on a running cluster.**
`catalog.yaml` is bundled into the `folia-nexa-mgmt` snap at build time
(`mgmt/src/folia_mgmt/catalog.yaml` → packaged into
`lib/python3.12/site-packages/folia_mgmt/catalog.yaml` — verified by
actually unpacking a built snap, see `CLAUDE.md`). A production mgmt
instance running an already-built snap won't see your merged entry
until whoever operates that cluster rebuilds and reinstalls
(`cd mgmt && snapcraft`, then `snap install ... --dangerous` again —
`CLAUDE.md` Phase 1). If you need it available sooner than that
cluster's next mgmt release, ask the operator to add it to their
`$SNAP_COMMON/plugin-catalog-override.yaml` in the meantime (PLAN.md
§14A) — that one *does* take effect immediately, no restart, since
`load_catalog` re-reads both files from disk on every request rather
than caching them. The merged PR is what makes it a permanent,
reviewed part of the catalog everyone gets by default; the override
file is what makes it available today.

## You're done

At this point your plugin is: built against the right Java/API version,
tested on a real Folia server, structured so its logic is testable
independent of Bukkit, correctly scheduled for regionized threading,
and either merged into the catalog or available via an operator's
override while that merge is pending. That's the whole loop — from part
1's `sudo apt install openjdk-21-jdk` to a game master (see
[`../game-master-howto.md`](../game-master-howto.md)) declaring
`--plugin <your-id>` on a world.
