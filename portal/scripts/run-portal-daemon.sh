#!/bin/sh
# Wrapper for the `daemon` app — lets `snap set folia-nexa-portal
# listen-port=<port>` / `listen-host=<host>` override
# FOLIA_PORTAL_LISTEN_PORT/FOLIA_PORTAL_LISTEN_HOST (serve.py) without
# hand-editing a systemd drop-in. snap/hooks/configure restarts this
# service whenever a setting changes; on every (re)start we just re-read
# them via snapctl and export them as the env vars serve.py expects.
set -e

port="$(snapctl get listen-port)"
if [ -n "$port" ]; then
  export FOLIA_PORTAL_LISTEN_PORT="$port"
fi

host="$(snapctl get listen-host)"
if [ -n "$host" ]; then
  export FOLIA_PORTAL_LISTEN_HOST="$host"
fi

exec "$SNAP/bin/folia-nexa-portal"
