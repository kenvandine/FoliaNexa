#!/bin/sh
# Wrapper for the `daemon` app — lets `snap set folia-nexa-mgmt
# listen-port=<port>` / `public-url=<url>` / `artifacts-base-url=<url>`
# override Settings.listen_port / Settings.public_url /
# Settings.artifacts_base_url (config.py) without hand-editing a systemd
# drop-in. snap/hooks/configure restarts this service whenever a setting
# changes; on every (re)start we just re-read them via snapctl and export
# them as the env vars pydantic-settings expects.
set -e

port="$(snapctl get listen-port)"
if [ -n "$port" ]; then
  export FOLIA_MGMT_LISTEN_PORT="$port"
fi

public_url="$(snapctl get public-url)"
if [ -n "$public_url" ]; then
  export FOLIA_MGMT_PUBLIC_URL="$public_url"
fi

artifacts_base_url="$(snapctl get artifacts-base-url)"
if [ -n "$artifacts_base_url" ]; then
  export FOLIA_MGMT_ARTIFACTS_BASE_URL="$artifacts_base_url"
fi

exec "$SNAP/bin/folia-nexa-mgmt" serve
