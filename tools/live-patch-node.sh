#!/usr/bin/env bash
#
# live-patch-node.sh — scp's a freshly-built folia-nexa-node snap to every
# LXD host and installs it into every already-running world container on
# each one, live. This is the manual workaround refresh-cluster-
# deployment.sh's own header comment documents (a rebuilt folia-node-base
# only affects worlds placed *after* the refresh — existing worlds keep
# running whatever node snap they launched with) — automated across every
# world on every host instead of one container at a time by hand.
#
# Confirmed the hard way this is needed: a world created a long time ago
# can be running a folia-nexa-node build old enough to predate the current
# sync_world_config design entirely (no re-sync on restart at all, just a
# one-time "already staged" no-op) — a plain container restart, or even
# refresh-cluster-deployment.sh's own folia-node-base refresh, does
# nothing for it. Only this actually gets new node code into it.
#
# This does NOT rebuild the snap for you — build it first
# (./tools/build-snap.sh node), same as refresh-cluster-deployment.sh's
# own build step.
#
# WARNING: `snap restart folia-nexa-node.daemon` takes the running
# Minecraft JVM down with it (it's a child process of the node-agent
# daemon; systemd's default KillMode=control-group kills the whole cgroup
# on restart) — every world this touches briefly disconnects its players,
# same disruption class as refresh-cluster-deployment.sh's proxy step.
# Confirms before proceeding unless -y/--yes.
#
# Defaults below point at the "apollo" cluster; override with flags or the
# matching env vars to point this at a different cluster entirely.

set -euo pipefail

PROG="$(basename "$0")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LXD_HOSTS="${LXD_HOSTS:-192.168.1.2,192.168.1.3,192.168.1.4}"
LXD_PROJECT="${LXD_PROJECT:-folia}"
NODE_SNAP="${NODE_SNAP:-}"
ASSUME_YES=""

usage() {
  cat <<EOF
Usage: $PROG [options]

Live-patches every already-running world container on every LXD host with
a freshly-built folia-nexa-node snap.

Options:
  --lxd-hosts H1,H2    Comma-separated LXD hosts to patch (default: $LXD_HOSTS)
  --lxd-project NAME   LXD project worlds live in on those hosts
                        (default: $LXD_PROJECT)
  --node-snap PATH     Path to the built folia-nexa-node snap (default:
                        the newest node/folia-nexa-node_*.snap in this repo)
  -y, --yes            Don't prompt before restarting every world's
                        node-agent (and, with it, every world's JVM)
  -h, --help            Show this help

Any HOST above may be user@host if the SSH user differs from your local
user.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lxd-hosts) LXD_HOSTS="$2"; shift 2 ;;
    --lxd-project) LXD_PROJECT="$2"; shift 2 ;;
    --node-snap) NODE_SNAP="$2"; shift 2 ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1 (see --help)" >&2; exit 1 ;;
  esac
done

if [[ -z "$NODE_SNAP" ]]; then
  NODE_SNAP="$(ls -t "$REPO_ROOT"/node/folia-nexa-node_*.snap 2>/dev/null | head -1 || true)"
fi
if [[ -z "$NODE_SNAP" || ! -f "$NODE_SNAP" ]]; then
  echo "ERROR: no node snap found — build one first: ./tools/build-snap.sh node" >&2
  echo "       (or pass --node-snap /path/to/folia-nexa-node_*.snap)" >&2
  exit 1
fi

for cmd in ssh scp; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: $cmd not found" >&2; exit 1; }
done

echo "Will patch every running world container on: $LXD_HOSTS (project '$LXD_PROJECT')"
echo "Using snap: $NODE_SNAP"
if [[ -z "$ASSUME_YES" ]]; then
  read -r -p "This restarts folia-nexa-node.daemon on every matching container, disconnecting its players. Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]] || { echo "Aborted."; exit 1; }
fi

# Same connection-reuse caveat as refresh-cluster-deployment.sh: a pty
# requested on a multiplexed connection whose *master* wasn't opened with
# one can get allocated at the protocol level without actually being
# wired to your terminal, silently eating a sudo password prompt. Every
# sudo-requiring call below forces a fresh, non-multiplexed connection.
SSH_T=(ssh -t -o ControlMaster=no -o ControlPath=none)
SSH_PLAIN=(ssh -o ControlMaster=no -o ControlPath=none)

# Same "stage under $HOME, not /tmp" reasoning as refresh-cluster-
# deployment.sh — a size-capped /tmp on some hosts has run out of room
# mid-transfer before on a snap big enough to hit that ceiling.
REMOTE_TMP_DIRS=()

cleanup_remote_tmp_dirs() {
  local entry host dir
  for entry in "${REMOTE_TMP_DIRS[@]:-}"; do
    [[ -z "$entry" ]] && continue
    host="${entry%%|*}"
    dir="${entry#*|}"
    "${SSH_PLAIN[@]}" "$host" "rm -rf -- '$dir'" 2>/dev/null || true
  done
}
trap cleanup_remote_tmp_dirs EXIT

make_remote_tmp_dir() {
  "${SSH_PLAIN[@]}" "$1" 'mktemp -d "$HOME/.folia-live-patch.XXXXXX"'
}

overall_failed=""

IFS=',' read -ra hosts <<< "$LXD_HOSTS"
for host in "${hosts[@]}"; do
  echo "==> Patching worlds on $host"
  TMP_DIR="$(make_remote_tmp_dir "$host")"
  REMOTE_TMP_DIRS+=("$host|$TMP_DIR")
  scp "$NODE_SNAP" "$host:$TMP_DIR/"
  scp "$REPO_ROOT/tools/live-patch-node-remote.sh" "$host:$TMP_DIR/"
  if ! "${SSH_T[@]}" "$host" "sudo bash '$TMP_DIR/live-patch-node-remote.sh' '$LXD_PROJECT' '$TMP_DIR/$(basename "$NODE_SNAP")'"; then
    echo "==> $host reported at least one failed container (see output above)"
    overall_failed=1
  fi
  "${SSH_PLAIN[@]}" "$host" "rm -rf -- '$TMP_DIR'"
done

echo "==> Done."
[[ -z "$overall_failed" ]]
