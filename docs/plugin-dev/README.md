# Plugin Development How-To Series

A from-scratch guide for developing an in-house plugin for FoliaNexa —
written for someone who has never built a Minecraft server plugin
before. If you've done Bukkit/Paper plugin dev before but are new to
*Folia* specifically, start at part 2 — part 1 is standard Java/Gradle
environment setup you likely already have.

1. **[Setting up your development environment (Ubuntu)](01-environment-setup.md)**
   — JDK 21, an IDE, the Gradle wrapper, scaffolding a new plugin
   project, and running a local Folia server to test against
   (§1.8's `tools/folia-nexa-spawn.sh` is the recommended way to do
   that day to day).
2. **[Writing a sound, well-architected plugin](02-plugin-architecture.md)**
   — project structure, `plugin.yml`, and Folia's region-scheduler APIs
   (`RegionScheduler`/`GlobalRegionScheduler`/`AsyncScheduler`) that
   replace the legacy Bukkit scheduler every older tutorial teaches.
3. **[Submitting your plugin for review](03-submitting-for-review.md)**
   — cutting a release, writing a `catalog.yaml` entry, a self-review
   checklist, and what actually happens (and doesn't) once a PR merges.

## Where this fits

This series is about *building* a plugin. For picking existing catalog
plugins and deploying a world that uses them (no development required),
see [`../game-master-howto.md`](../game-master-howto.md) instead. For
the catalog's own design — how entries are structured, merged with
operator overrides, and turned into a world's plugin manifest — see
`PLAN.md` §14A and §14B.

**Using Claude Code to actually write one?** The
[`folia-plugin-scaffold`](../../.claude/skills/folia-plugin-scaffold/)
skill automates this whole series — scaffolds a real, building plugin
project (the Gradle/`plugin.yml`/Folia-scheduler skeleton this series
describes), either from a feature description or from a Modrinth
mod/plugin link, and walks through to a catalog entry per part 3.
