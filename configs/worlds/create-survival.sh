#!/usr/bin/env bash
# Declares the main survival world. Sized to PLAN.md §17's reference host
# allocation (6 P-cores / 12GB) — adjust --cpu/--memory/--labels for yours.
#
# Plugin set matches PLAN.md §14's curated matrix: claims, economy,
# auctions, RPG skills, custom items/bosses, homes/portals, voice chat,
# diagnostics, web map, and a Discord chat bridge. See
# configs/plugins/manifests/world-survival.json for the manifest this
# implies — mgmt only downloads it once folia-nexa-mgmt's artifacts_base_url
# (or whatever host you're using) actually serves that file, so populate
# the placeholder URLs in there with real ones before this world can
# finish provisioning.
set -euo pipefail

folia-nexa-mgmt worlds create world-survival \
  --type overworld \
  --cpu 6 \
  --memory 12GB \
  --labels cpu_type=p-core \
  --plugin LuckPerms \
  --plugin HuskClaims \
  --plugin Vault-Unlocked \
  --plugin AxAuctions \
  --plugin AuraSkills \
  --plugin ItemsAdder \
  --plugin MythicMobs \
  --plugin HuskHomes \
  --plugin HuskPortals \
  --plugin SimpleVoiceChat \
  --plugin Spark \
  --plugin BlueMap \
  --plugin DiscordSRV \
  "$@"
