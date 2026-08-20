#!/usr/bin/env bash
#
# live-patch-node-remote.sh — runs ON an LXD host (not your workstation):
# installs a freshly-built folia-nexa-node snap into every already-running
# world container on this host, live, without waiting for those worlds to
# be recreated from a refreshed folia-node-base image. Called by
# live-patch-node.sh via scp + ssh, one host at a time; not meant to be run
# by hand unless you're patching a single host outside that script.
#
# This is the manual live-patch workaround refresh-cluster-deployment.sh's
# own header comment documents (refreshing folia-node-base only affects
# worlds placed *after* the refresh) — this script just automates running
# it across every world on a host instead of one container at a time by
# hand.
#
# Usage: live-patch-node-remote.sh <lxd-project> <path-to-node-snap>

set -uo pipefail  # not -e: one container's failure must not abort the rest

LXD_PROJECT="${1:?usage: $0 <lxd-project> <path-to-node-snap>}"
SNAP_FILE="${2:?usage: $0 <lxd-project> <path-to-node-snap>}"
IN_CONTAINER_SNAP="/root/$(basename "$SNAP_FILE")"

# Only RUNNING containers — a stopped/pending world has no folia-nexa-node
# daemon to patch, and `lxc exec`/`lxc file push` against one would just
# fail. Anything else (e.g. mid-migration) is skipped the same way, not
# specially handled — this is a best-effort live patch, not a scheduler.
mapfile -t containers < <(
  sudo lxc list --project "$LXD_PROJECT" --format csv --columns n,s |
    awk -F, '$2 == "RUNNING" { print $1 }'
)

if [ "${#containers[@]}" -eq 0 ]; then
  echo "no running containers found in project '$LXD_PROJECT' on $(hostname) — nothing to patch"
  exit 0
fi

echo "--- found ${#containers[@]} running container(s) in '$LXD_PROJECT': ${containers[*]}"

patched=()
failed=()

for container in "${containers[@]}"; do
  echo "--- patching $container"

  if ! sudo lxc file push "$SNAP_FILE" "$container$IN_CONTAINER_SNAP" --project "$LXD_PROJECT"; then
    echo "    FAILED: could not push snap to $container"
    failed+=("$container")
    continue
  fi

  # `snap install` on a name that's already installed errors out rather
  # than upgrading in place — fall back to remove+install rather than
  # treating that as a real failure, same "don't let this one break the
  # loop" spirit as everything else here.
  if ! sudo lxc exec "$container" --project "$LXD_PROJECT" -- \
      snap install "$IN_CONTAINER_SNAP" --dangerous --devmode </dev/null 2>/tmp/live-patch-install-err; then
    if grep -q "already installed" /tmp/live-patch-install-err 2>/dev/null; then
      sudo lxc exec "$container" --project "$LXD_PROJECT" -- snap remove folia-nexa-node </dev/null >/dev/null 2>&1
      if ! sudo lxc exec "$container" --project "$LXD_PROJECT" -- \
          snap install "$IN_CONTAINER_SNAP" --dangerous --devmode </dev/null; then
        echo "    FAILED: snap install (after remove+retry) failed on $container"
        failed+=("$container")
        rm -f /tmp/live-patch-install-err
        continue
      fi
    else
      echo "    FAILED: snap install failed on $container:"
      sed 's/^/      /' /tmp/live-patch-install-err
      failed+=("$container")
      rm -f /tmp/live-patch-install-err
      continue
    fi
  fi
  rm -f /tmp/live-patch-install-err

  if ! sudo lxc exec "$container" --project "$LXD_PROJECT" -- \
      snap restart folia-nexa-node.daemon </dev/null; then
    echo "    FAILED: could not restart folia-nexa-node.daemon on $container"
    failed+=("$container")
    continue
  fi

  sudo lxc exec "$container" --project "$LXD_PROJECT" -- rm -f "$IN_CONTAINER_SNAP" </dev/null 2>/dev/null || true
  echo "    OK"
  patched+=("$container")
done

echo "--- done on $(hostname): ${#patched[@]} patched, ${#failed[@]} failed"
[ "${#failed[@]}" -gt 0 ] && echo "    failed: ${failed[*]}"
[ "${#failed[@]}" -eq 0 ]
