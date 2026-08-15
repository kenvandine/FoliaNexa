#!/usr/bin/env bash
# BedWars: team objective PvP, sized as a lightweight E-core minigame per
# PLAN.md §17's reference allocation. Every --plugin below is a
# mgmt/src/folia_mgmt/catalog.yaml id; populate any with a placeholder
# download_url before this world can finish provisioning — same
# Folia-compatibility caveat as the SkyWars world: verify BedWars1058 (or
# whichever fork you pick) against the Folia version you're running
# before relying on it.
set -euo pipefail

folia-nexa-mgmt worlds create world-minigame-bedwars \
  --type minigame \
  --cpu 2 \
  --memory 3GB \
  --labels cpu_type=e-core \
  --plugin LuckPerms \
  --plugin BedWars1058 \
  --plugin Vault-Unlocked \
  --plugin FancyHolograms \
  --plugin Spark \
  --plugin FoliaNexaStats \
  "$@"
