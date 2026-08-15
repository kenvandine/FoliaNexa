#!/bin/bash
set -euo pipefail
CREDENTIALS_FILE="$SNAP_COMMON/credentials.env"
if [ ! -f "$CREDENTIALS_FILE" ]; then
  echo "No credentials file yet — has folia-nexa-db.daemon started at least once?" >&2
  exit 1
fi
cat "$CREDENTIALS_FILE"
