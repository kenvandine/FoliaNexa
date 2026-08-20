#!/usr/bin/env bash
# Declares the starter world set: one survival world. Requires
# `folia-nexa-mgmt login <url> <user> <pass>` to have been run already (or
# pass --mgmt-url on every world script this calls, which forwards any
# extra args you give this script to each of them).
#
# The two example minigame worlds (SkyWars/BedWars) that used to live
# alongside this one were retired along with their catalog entries
# (SkyWarsReloaded, BedWars1058, FancyHolograms, FancyNpcs) — see
# docs/game-master-howto.md for how to declare a minigame world of your
# own from scratch with plugins you've actually vetted for Folia.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

./create-survival.sh "$@"

folia-nexa-mgmt worlds list "$@"
