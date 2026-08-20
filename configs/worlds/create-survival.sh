#!/usr/bin/env bash
# Declares the main survival world. Sized to PLAN.md §17's reference host
# allocation (6 P-cores / 12GB) — adjust --cpu/--memory/--labels for yours.
#
# Plugin set matches PLAN.md §14's curated matrix: claims, economy, RPG
# skills, homes/portals, voice chat, and a Discord chat bridge. Every
# --plugin below is a mgmt/src/folia_mgmt/catalog.yaml id ('folia-nexa-mgmt
# plugins list' to browse); several are placeholders with no download_url
# yet, so populate those in catalog.yaml (or a plugin-catalog-override.yaml)
# before this world can finish provisioning.
set -euo pipefail

folia-nexa-mgmt worlds create world-survival \
  --type overworld \
  --cpu 6 \
  --memory 12GB \
  --labels cpu_type=p-core \
  --plugin LuckPerms \
  --plugin HuskClaims \
  --plugin Vault-Unlocked \
  --plugin AuraSkills \
  --plugin HuskHomes \
  --plugin HuskPortals \
  --plugin SimpleVoiceChat \
  --plugin DiscordSRV \
  --plugin FoliaNexaStats \
  "$@"
