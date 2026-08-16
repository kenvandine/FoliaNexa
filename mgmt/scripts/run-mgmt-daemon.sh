#!/bin/sh
# Wrapper for the `daemon` app — lets `snap set folia-nexa-mgmt
# listen-port=<port>` override Settings.listen_port (config.py) without
# hand-editing a systemd drop-in. snap/hooks/configure restarts this
# service whenever the setting changes; on every (re)start we just re-read
# it via snapctl and export it as the env var pydantic-settings expects.
set -e

port="$(snapctl get listen-port)"
if [ -n "$port" ]; then
  export FOLIA_MGMT_LISTEN_PORT="$port"
fi

exec "$SNAP/bin/folia-nexa-mgmt" serve
