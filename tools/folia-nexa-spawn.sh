#!/usr/bin/env bash
#
# folia-nexa-spawn.sh — spin up a local, single-machine Folia server for
# fast plugin iteration, with one or more in-development plugins built
# from local source and dropped straight into plugins/. Automates the
# manual loop documented in docs/plugin-dev/01-environment-setup.md
# (§1.6-1.7) — read that doc first if you want to understand what this
# script is doing under the hood, or if you'd rather do it by hand.
#
# No FoliaNexa cluster, mgmt, LXD, or proxy involved — this downloads a
# real Folia server jar from PaperMC and runs it directly on this
# machine, offline-mode, for one developer to connect a client to.
#
# Usage:
#   tools/folia-nexa-spawn.sh <version> <world-type> [options]
#
# Example:
#   tools/folia-nexa-spawn.sh 1.21.4 overworld \
#     --plugindir=~/src/my-folianexa-plugin
#
# Required positionals:
#   version         Folia version to run, e.g. 1.21.4 — passed straight
#                    to fill.papermc.io/v3, same as the doc's manual steps.
#   world-type      Label for this test world (overworld, nether, end,
#                    lobby, minigame, proxy, staging, infra — matches
#                    mgmt's WorldType, mgmt/src/folia_mgmt/models.py — but
#                    any string is accepted, just a label + save dir key).
#                    lobby/minigame/staging get a flat superflat world
#                    (level-type=flat) since those are normally hand-built,
#                    not generated; everything else gets normal generation.
#
# Options:
#   --plugindir=PATH   Local Gradle project dir for a plugin under
#                        development. Runs ./gradlew build in it and
#                        copies the resulting jar into plugins/. Repeat
#                        for more than one plugin.
#   --no-build          Skip ./gradlew build — just (re-)copy whatever
#                        jar is already in each --plugindir's build/libs/.
#                        Faster relaunch when only server-side config or
#                        another plugin changed, not this one's code.
#   --port=PORT          Fixed port instead of an auto-picked free one.
#   --memory=SIZE         -Xms/-Xmx value, e.g. 4G (default: 2G).
#   --workdir=PATH         Server save directory (default: a per
#                        version+world-type dir under
#                        ~/.local/share/folia-nexa-spawn, reused across
#                        runs so the world and downloaded jar persist).
#   --clean                Wipe the world save (world/, world_nether/,
#                        world_the_end/) before starting — fresh terrain,
#                        keeps the downloaded server jar and properties.
#   --refresh-jar           Re-download the server jar even if cached.
#   --online-mode            Real Mojang auth instead of the default
#                        offline-mode (offline is what the setup doc
#                        recommends for local-only testing).
#   -h, --help
#
# The server runs in the foreground with console I/O attached, same as
# running `java -jar folia-server.jar --nogui` by hand — type server
# commands directly, Ctrl+C or `/stop` shuts it down cleanly. Once the
# log reports the server is up, this script prints the address to
# connect a client to.

set -euo pipefail

PROG="$(basename "$0")"

usage() {
  sed -n '2,55p' "$0" | sed 's/^# \?//'
}

VERSION=""
WORLD_TYPE=""
PLUGIN_DIRS=()
NO_BUILD="false"
PORT=""
MEMORY="2G"
WORKDIR=""
CLEAN="false"
REFRESH_JAR="false"
ONLINE_MODE="false"

# Positionals first.
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --plugindir=*) PLUGIN_DIRS+=("${1#--plugindir=}"); shift ;;
    --no-build) NO_BUILD="true"; shift ;;
    --port=*) PORT="${1#--port=}"; shift ;;
    --memory=*) MEMORY="${1#--memory=}"; shift ;;
    --workdir=*) WORKDIR="${1#--workdir=}"; shift ;;
    --clean) CLEAN="true"; shift ;;
    --refresh-jar) REFRESH_JAR="true"; shift ;;
    --online-mode) ONLINE_MODE="true"; shift ;;
    --*) echo "$PROG: unknown option: $1" >&2; usage >&2; exit 1 ;;
    *)
      if [[ -z "$VERSION" ]]; then VERSION="$1";
      elif [[ -z "$WORLD_TYPE" ]]; then WORLD_TYPE="$1";
      else echo "$PROG: unexpected argument: $1" >&2; exit 1; fi
      shift
      ;;
  esac
done

if [[ -z "$VERSION" || -z "$WORLD_TYPE" ]]; then
  echo "$PROG: version and world-type are required" >&2
  usage >&2
  exit 1
fi

case "$WORLD_TYPE" in
  overworld|nether|end|lobby|minigame|proxy|staging|infra) ;;
  *)
    echo "$PROG: warning: '$WORLD_TYPE' isn't one of mgmt's WorldType" \
         "values (overworld/nether/end/lobby/minigame/proxy/staging/infra)" \
         "— using it as a free-form label anyway." >&2
    ;;
esac

case "$WORLD_TYPE" in
  lobby|minigame|staging) LEVEL_TYPE="flat" ;;
  *) LEVEL_TYPE="" ;;
esac

if ! command -v java >/dev/null 2>&1; then
  echo "$PROG: no 'java' on PATH — see docs/plugin-dev/01-environment-setup.md §1.1" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  echo "$PROG: needs both 'curl' and 'python3' on PATH" >&2
  exit 1
fi

if [[ -z "$WORKDIR" ]]; then
  WORKDIR="${XDG_DATA_HOME:-$HOME/.local/share}/folia-nexa-spawn/${VERSION}-${WORLD_TYPE}"
fi
mkdir -p "$WORKDIR/plugins"
WORKDIR="$(cd "$WORKDIR" && pwd)"

LOCK_FILE="$WORKDIR/.spawn.lock"
if [[ -f "$LOCK_FILE" ]]; then
  OLD_PID="$(cat "$LOCK_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "$PROG: a server already appears to be running for this" \
         "version+world-type (pid $OLD_PID, $WORKDIR)." >&2
    echo "Use --workdir to run a second, separate instance." >&2
    exit 1
  fi
  rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
cleanup() {
  rm -f "$LOCK_FILE"
  [[ -n "${WATCHER_PID:-}" ]] && kill "$WATCHER_PID" 2>/dev/null || true
}
trap cleanup EXIT

if [[ "$CLEAN" == "true" ]]; then
  echo "$PROG: --clean: wiping world save in $WORKDIR"
  rm -rf "$WORKDIR/world" "$WORKDIR/world_nether" "$WORKDIR/world_the_end"
fi

SERVER_JAR="$WORKDIR/folia-server.jar"
if [[ ! -f "$SERVER_JAR" || "$REFRESH_JAR" == "true" ]]; then
  echo "$PROG: fetching Folia $VERSION from fill.papermc.io ..."
  BUILD_JSON="$(curl -sf "https://fill.papermc.io/v3/projects/folia/versions/${VERSION}/builds")"
  DOWNLOAD_URL="$(python3 -c '
import json, sys
builds = json.load(sys.stdin)
if not builds:
    sys.exit("no builds found for this version")
print(builds[0]["downloads"]["server:default"]["url"])
' <<<"$BUILD_JSON")"
  curl -Lf -o "$SERVER_JAR.tmp" "$DOWNLOAD_URL"
  mv "$SERVER_JAR.tmp" "$SERVER_JAR"
else
  echo "$PROG: reusing cached server jar at $SERVER_JAR (--refresh-jar to re-fetch)"
fi

echo "eula=true" > "$WORKDIR/eula.txt"

if [[ -z "$PORT" ]]; then
  PORT="$(python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
')"
fi

python3 - "$WORKDIR/server.properties" "$PORT" "$LEVEL_TYPE" "$ONLINE_MODE" "$WORLD_TYPE" <<'PYEOF'
import sys

path, port, level_type, online_mode, world_type = sys.argv[1:6]

overrides = {
    "server-port": port,
    "online-mode": online_mode,
    "difficulty": "peaceful",
    "spawn-protection": "0",
    "motd": f"folia-nexa-spawn dev server ({world_type})",
    "level-type": level_type if level_type else "default",
}

lines = []
try:
    with open(path) as f:
        lines = f.readlines()
except FileNotFoundError:
    pass

seen = set()
out = []
for line in lines:
    stripped = line.rstrip("\n")
    if "=" in stripped and not stripped.startswith("#"):
        key = stripped.split("=", 1)[0]
        if key in overrides:
            out.append(f"{key}={overrides[key]}\n")
            seen.add(key)
            continue
    out.append(line)
for key, value in overrides.items():
    if key not in seen:
        out.append(f"{key}={value}\n")

with open(path, "w") as f:
    f.writelines(out)
PYEOF

for plugindir in "${PLUGIN_DIRS[@]}"; do
  plugindir="$(cd "$plugindir" && pwd)"
  name="$(basename "$plugindir")"
  if [[ ! -x "$plugindir/gradlew" ]]; then
    echo "$PROG: $plugindir has no ./gradlew — not a Gradle plugin project?" >&2
    exit 1
  fi
  if [[ "$NO_BUILD" != "true" ]]; then
    echo "$PROG: building $name ..."
    ( cd "$plugindir" && ./gradlew -q build )
  fi
  jar="$(find "$plugindir/build/libs" -maxdepth 1 -name '*.jar' \
      ! -name '*-plain.jar' ! -name '*-sources.jar' ! -name '*-javadoc.jar' \
      -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n1 | cut -d' ' -f2-)"
  if [[ -z "$jar" ]]; then
    echo "$PROG: no built jar found in $plugindir/build/libs — did the build produce one?" >&2
    exit 1
  fi
  cp "$jar" "$WORKDIR/plugins/"
  echo "$PROG: loaded plugin $name <- $(basename "$jar")"
done

# Folia/Paper write their own console log to logs/latest.log; watch that
# rather than re-capturing java's own stdout, so the console stays a
# normal attached TTY (server commands, Ctrl+C, etc. all just work).
LOG_FILE="$WORKDIR/logs/latest.log"
mkdir -p "$WORKDIR/logs"
rm -f "$LOG_FILE"

(
  for _ in $(seq 1 600); do
    if grep -q 'Done (' "$LOG_FILE" 2>/dev/null; then
      echo ""
      echo "=============================================================="
      echo " Folia $VERSION ($WORLD_TYPE) is up."
      echo " Connect to your world with localhost:$PORT"
      if [[ "$ONLINE_MODE" != "true" ]]; then
        echo " (offline-mode — any username works)"
      fi
      echo " World save + logs: $WORKDIR"
      echo " Stop the server with /stop in this console, or Ctrl+C."
      echo "=============================================================="
      echo ""
      break
    fi
    sleep 1
  done
) &
WATCHER_PID=$!

echo "$PROG: starting Folia $VERSION ($WORLD_TYPE) in $WORKDIR ..."
cd "$WORKDIR"
java -Xms"$MEMORY" -Xmx"$MEMORY" -jar folia-server.jar --nogui
