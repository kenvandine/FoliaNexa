#!/usr/bin/env bash
#
# refresh-node-base-image.sh — runs ON an LXD host (not your workstation):
# rebuilds that host's folia-node-base image from the current
# folia-node-base plus a freshly-installed folia-nexa-node snap, and
# repoints the alias at it. Called by refresh-cluster-deployment.sh via scp
# + ssh, one host at a time; not meant to be run by hand unless you're
# refreshing a single host outside that script.
#
# Usage: refresh-node-base-image.sh <lxd-project> <path-to-node-snap>

set -euo pipefail

LXD_PROJECT="${1:?usage: $0 <lxd-project> <path-to-node-snap>}"
SNAP_FILE="${2:?usage: $0 <lxd-project> <path-to-node-snap>}"
CONTAINER="tmp-node-rebuild-$$"

cleanup() {
  sudo lxc delete "$CONTAINER" --project "$LXD_PROJECT" --force >/dev/null 2>&1 || true
}
trap cleanup EXIT

OLD_FINGERPRINT="$(sudo lxc image list folia-node-base --project "$LXD_PROJECT" --format csv --columns f 2>/dev/null | head -1 || true)"

echo "--- launching throwaway container $CONTAINER from the current folia-node-base"
sudo lxc launch folia-node-base "$CONTAINER" --project "$LXD_PROJECT" -c limits.cpu=2 -c limits.memory=2GB

echo "--- waiting for it to come up"
for _ in $(seq 1 30); do
  sudo lxc exec "$CONTAINER" --project "$LXD_PROJECT" -- true </dev/null 2>/dev/null && break
  sleep 1
done

# A working shell doesn't mean snapd is ready yet — `snap install` right
# after boot can hit "dial unix /run/snapd.socket: no such file or
# directory" if snapd is still initializing. Wait for it specifically.
echo "--- waiting for snapd to be ready"
for _ in $(seq 1 30); do
  sudo lxc exec "$CONTAINER" --project "$LXD_PROJECT" -- snap version </dev/null >/dev/null 2>&1 && break
  sleep 1
done

# No world was ever assigned to this throwaway container (no user.folia.*
# config), so the old node snap never staged anything real — safe to strip
# and replace before it's ever baked into the published image.
sudo lxc exec "$CONTAINER" --project "$LXD_PROJECT" -- snap stop folia-nexa-node.daemon </dev/null >/dev/null 2>&1 || true
sudo lxc exec "$CONTAINER" --project "$LXD_PROJECT" -- snap remove folia-nexa-node </dev/null >/dev/null 2>&1 || true
sudo lxc file push "$SNAP_FILE" "$CONTAINER$SNAP_FILE" --project "$LXD_PROJECT"
sudo lxc exec "$CONTAINER" --project "$LXD_PROJECT" -- snap install "$SNAP_FILE" --dangerous --devmode </dev/null

echo "--- verifying no world save data got staged"
if sudo lxc exec "$CONTAINER" --project "$LXD_PROJECT" -- test -e /var/snap/folia-nexa-node/common/world </dev/null; then
  echo "ERROR: found world data in the throwaway container — refusing to publish it as the base image" >&2
  exit 1
fi

sudo lxc stop "$CONTAINER" --project "$LXD_PROJECT"

echo "--- publishing"
PUBLISH_ERR="$(mktemp)"
if ! sudo lxc publish "$CONTAINER" --project "$LXD_PROJECT" --alias folia-node-base --reuse 2>"$PUBLISH_ERR"; then
  if grep -q "already exists" "$PUBLISH_ERR"; then
    # Byte-identical to a prior publish (no real node change since last
    # refresh) — LXD dedupes by content hash, so just repoint the alias.
    FPRINT="$(grep -oE '[0-9a-f]{64}' "$PUBLISH_ERR" | head -1)"
    echo "--- image unchanged (already published as $FPRINT) — repointing the alias"
    sudo lxc image alias delete folia-node-base --project "$LXD_PROJECT" 2>/dev/null || true
    sudo lxc image alias create folia-node-base "$FPRINT" --project "$LXD_PROJECT"
  else
    cat "$PUBLISH_ERR" >&2
    rm -f "$PUBLISH_ERR"
    exit 1
  fi
fi
rm -f "$PUBLISH_ERR"

NEW_FINGERPRINT="$(sudo lxc image list folia-node-base --project "$LXD_PROJECT" --format csv --columns f 2>/dev/null | head -1 || true)"
if [ -n "$OLD_FINGERPRINT" ] && [ "$OLD_FINGERPRINT" != "$NEW_FINGERPRINT" ]; then
  echo "--- deleting superseded image $OLD_FINGERPRINT"
  sudo lxc image delete "$OLD_FINGERPRINT" --project "$LXD_PROJECT" 2>/dev/null || true
fi

echo "--- done: folia-node-base is now $NEW_FINGERPRINT"
