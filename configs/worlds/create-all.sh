#!/usr/bin/env bash
# Declares the starter world set: one survival world, two minigames.
# Requires `folia-nexa-mgmt login <url> <user> <pass>` to have been run
# already (or pass --mgmt-url on every world script this calls, which
# forwards any extra args you give this script to each of them).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

./create-survival.sh "$@"
./create-minigame-skywars.sh "$@"
./create-minigame-bedwars.sh "$@"

folia-nexa-mgmt worlds list "$@"
