#!/usr/bin/env bash
# SkyWars: PvP islands with kit shops, sized as a lightweight E-core
# minigame per PLAN.md §17's reference allocation. See
# configs/plugins/manifests/world-minigame-skywars.json for the manifest
# this implies (populate the placeholder plugin URLs before it can finish
# provisioning) — and verify SkyWarsReloaded's Folia compatibility for
# whatever version you pin; classic Bukkit-era minigame plugins commonly
# assume single-threaded world ticking that Folia's regionized scheduler
# doesn't guarantee.
set -euo pipefail

folia-smp-mgmt worlds create world-minigame-skywars \
  --type minigame \
  --cpu 2 \
  --memory 3GB \
  --labels cpu_type=e-core \
  --plugin LuckPerms \
  --plugin SkyWarsReloaded \
  --plugin Vault-Unlocked \
  --plugin FancyHolograms \
  --plugin FancyNpcs \
  --plugin Spark \
  "$@"
