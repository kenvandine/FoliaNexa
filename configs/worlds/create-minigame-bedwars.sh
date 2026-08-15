#!/usr/bin/env bash
# BedWars: team objective PvP, sized as a lightweight E-core minigame per
# PLAN.md §17's reference allocation. See
# configs/plugins/manifests/world-minigame-bedwars.json for the manifest
# this implies (populate the placeholder plugin URLs before it can finish
# provisioning) — same Folia-compatibility caveat as the SkyWars world:
# verify BedWars1058 (or whichever fork you pick) against the Folia
# version you're running before relying on it.
set -euo pipefail

folia-smp-mgmt worlds create world-minigame-bedwars \
  --type minigame \
  --cpu 2 \
  --memory 3GB \
  --labels cpu_type=e-core \
  --plugin LuckPerms \
  --plugin BedWars1058 \
  --plugin Vault-Unlocked \
  --plugin FancyHolograms \
  --plugin Spark \
  "$@"
