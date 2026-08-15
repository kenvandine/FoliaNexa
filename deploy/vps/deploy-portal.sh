#!/usr/bin/env bash
#
# deploy-portal.sh — sync portal/'s static files to the VPS's Caddy
# file_server root. No build step, no snapcraft.yaml (see portal/README.md
# for why) — this rsync is the entire deploy story.

set -euo pipefail

PROG="$(basename "$0")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

VPS_HOST=""
REMOTE_ROOT="/srv/folianexa-portal"

usage() {
  cat <<EOF
Usage: $PROG --vps-host <user@host> [options]

Required:
  --vps-host HOST     SSH destination for the VPS, e.g. root@203.0.113.5

Options:
  --remote-root PATH  Where Caddy's PORTAL_ROOT points on the VPS
                        (default: /srv/folianexa-portal — must match the
                        Caddyfile's PORTAL_ROOT/default)
  -h, --help           Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vps-host) VPS_HOST="$2"; shift 2 ;;
    --remote-root) REMOTE_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1 (see --help)" >&2; exit 1 ;;
  esac
done

[[ -z "$VPS_HOST" ]] && { echo "ERROR: --vps-host is required (see --help)" >&2; exit 1; }
command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync not found" >&2; exit 1; }

ssh "$VPS_HOST" "mkdir -p '$REMOTE_ROOT'"
rsync -avz --delete "$REPO_ROOT/portal/" "$VPS_HOST:$REMOTE_ROOT/"
echo "==> Synced portal/ to ${VPS_HOST}:${REMOTE_ROOT}"
