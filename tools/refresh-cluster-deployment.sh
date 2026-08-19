#!/usr/bin/env bash
#
# refresh-cluster-deployment.sh — rebuild folia-nexa-mgmt/-node/-proxy/
# -portal from this checkout and push the result to a running cluster:
# installs the new mgmt snap on the mgmt host, rebuilds every LXD host's
# folia-node-base image (PLAN.md §17 Phase 2 step 3) so *new* worlds pick
# up node changes, installs the new portal snap on the portal host, and
# (with confirmation, since it's public-facing) installs the new proxy
# snap.
#
# This is a *refresh* tool, not first-time bootstrap — it assumes
# folia-nexa-mgmt is already installed on the mgmt host, folia-node-base
# already exists as an alias on every LXD host (CLAUDE.md Phases 1 and 4),
# and folia-nexa-portal is already installed on the portal host (CLAUDE.md
# Phase 9). For a brand new cluster, follow CLAUDE.md's bootstrap phases
# once first.
#
# What this does NOT do:
#   - Touch any already-running world's container. A rebuilt
#     folia-node-base only affects worlds placed *after* the refresh —
#     existing worlds keep running whatever node snap they were launched
#     with until they're next recreated (or live-patched by hand: `lxc
#     file push` the snap into the container + `snap install --dangerous
#     --devmode` + `snap restart folia-nexa-node.daemon`).
#   - Touch folia-nexa-bot or folia-nexa-db — not part of this refresh.
#   - Push anything without asking first for the proxy step, since that's
#     the one public-facing piece real players may be connected to.
#     (folia-nexa-portal doesn't get the same treatment — reinstalling it
#     just briefly restarts a static-file server, not something any
#     connected player notices.)
#
# Prerequisites on every target host: passwordless sudo for the SSH user
# (or just run this from a real terminal — every remote sudo call uses
# `ssh -t` so it can prompt you for a password interactively if needed).
#
# Defaults below point at the "apollo" cluster; override with flags or the
# matching env vars to point this at a different cluster entirely.

set -euo pipefail

PROG="$(basename "$0")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MGMT_HOST="${MGMT_HOST:-192.168.1.2}"
LXD_HOSTS="${LXD_HOSTS:-192.168.1.2,192.168.1.3,192.168.1.4}"
LXD_PROJECT="${LXD_PROJECT:-folia}"
PROXY_HOST="${PROXY_HOST:-play.sullivan.linuxgroove.com}"
# Defaults to the same box as the proxy — that's this cluster's actual VPS
# edge topology (PLAN.md §7A: proxy and the portal both live on the VPS,
# only mgmt stays home) — override if yours differs.
PORTAL_HOST="${PORTAL_HOST:-$PROXY_HOST}"

SKIP_BUILD=""
SKIP_MGMT=""
SKIP_NODE=""
SKIP_PROXY=""
SKIP_PORTAL=""
ASSUME_YES=""

usage() {
  cat <<EOF
Usage: $PROG [options]

Rebuilds and redeploys folia-nexa-mgmt/-node/-proxy/-portal to a running
cluster.

Options:
  --mgmt-host HOST     mgmt install target (default: $MGMT_HOST)
  --lxd-hosts H1,H2    Comma-separated LXD hosts to refresh folia-node-base
                        on (default: $LXD_HOSTS)
  --lxd-project NAME   LXD project worlds live in on those hosts
                        (default: $LXD_PROJECT)
  --proxy-host HOST    proxy install target (default: $PROXY_HOST)
  --portal-host HOST   portal install target (default: same as --proxy-host)
  --skip-build         Reuse whatever *.snap files already exist in
                        mgmt/, node/, proxy/, portal/ instead of rebuilding
  --skip-mgmt          Don't touch the mgmt host
  --skip-node          Don't touch any LXD host / folia-node-base
  --skip-proxy         Don't touch the proxy host
  --skip-portal        Don't touch the portal host
  -y, --yes            Don't prompt before restarting the live proxy
  -h, --help           Show this help

Any HOST above may be user@host if the SSH user differs from your local
user.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mgmt-host) MGMT_HOST="$2"; shift 2 ;;
    --lxd-hosts) LXD_HOSTS="$2"; shift 2 ;;
    --lxd-project) LXD_PROJECT="$2"; shift 2 ;;
    --proxy-host) PROXY_HOST="$2"; shift 2 ;;
    --portal-host) PORTAL_HOST="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --skip-mgmt) SKIP_MGMT=1; shift ;;
    --skip-node) SKIP_NODE=1; shift ;;
    --skip-proxy) SKIP_PROXY=1; shift ;;
    --skip-portal) SKIP_PORTAL=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1 (see --help)" >&2; exit 1 ;;
  esac
done

for cmd in ssh scp snapcraft; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: $cmd not found" >&2; exit 1; }
done

# Every sudo-requiring ssh call below uses -t (so sudo can prompt for a
# password interactively) plus a forced fresh, non-multiplexed connection.
# If your ssh config reuses one connection across the scp calls and this
# ssh call (ControlMaster/ControlPersist), a pty requested on a connection
# whose *master* wasn't opened with one can get allocated at the protocol
# level without actually being wired to your terminal — the password
# prompt shows but nothing you type reaches it. Confirmed for real: this
# happened on the second host in a run, right after two scp calls to it.
SSH_T=(ssh -t -o ControlMaster=no -o ControlPath=none)
SSH_PLAIN=(ssh -o ControlMaster=no -o ControlPath=none)

# Snap files (proxy's especially — it bundles a full JRE, 250MB+) used to
# get scp'd to plain /tmp on every target host and never cleaned up
# afterward. Confirmed for real this broke a deploy outright: a VPS whose
# /tmp is a small, size-capped filesystem (unlike a big host's tmpfs) ran
# out of room mid-transfer on exactly the proxy snap, the one big enough
# to hit that ceiling while mgmt/node/portal's smaller ones never did.
# Every snap now goes to a fresh per-host directory under $HOME instead
# (ordinary disk, not /tmp, and not shared with anything else that might
# be filling /tmp) — created right before its scp, removed right after
# its install. REMOTE_TMP_DIRS + the EXIT trap below are a fallback for
# whichever directory was open when a `set -e` failure elsewhere cuts the
# script short, so a failed run doesn't still leave one behind.
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

# Prints the new directory's path on stdout — callers must separately
# append "$host|$dir" to REMOTE_TMP_DIRS themselves (this runs inside a
# command-substitution subshell, so an array append made in here would
# never be visible back in the main shell).
make_remote_tmp_dir() {
  "${SSH_PLAIN[@]}" "$1" 'mktemp -d "$HOME/.folia-deploy.XXXXXX"'
}

# -- build ---------------------------------------------------------------

if [[ -z "$SKIP_BUILD" ]]; then
  for part in mgmt node proxy portal; do
    [[ "$part" == "mgmt" && -n "$SKIP_MGMT" ]] && continue
    [[ "$part" == "node" && -n "$SKIP_NODE" ]] && continue
    [[ "$part" == "proxy" && -n "$SKIP_PROXY" ]] && continue
    [[ "$part" == "portal" && -n "$SKIP_PORTAL" ]] && continue
    echo "==> Building $part"
    # Full clean before every build, not just on failure — a snapcraft
    # managed build instance can be left with stale/corrupted part state
    # by an earlier interrupted build (seen for real: routes-sync-plugin's
    # Gradle wrapper going missing after a killed build, "Could not find
    # or load main class org.gradle.wrapper.GradleWrapperMain"). Costs a
    # full rebuild every run instead of an incremental one, which is the
    # right trade for a script meant to be run occasionally, not in a
    # tight dev loop.
    ( cd "$REPO_ROOT/$part" && snapcraft clean && rm -f ./*.snap && snapcraft )
  done
fi

# -- mgmt ------------------------------------------------------------------

if [[ -z "$SKIP_MGMT" ]]; then
  echo "==> Deploying mgmt to $MGMT_HOST"
  MGMT_TMP_DIR="$(make_remote_tmp_dir "$MGMT_HOST")"
  REMOTE_TMP_DIRS+=("$MGMT_HOST|$MGMT_TMP_DIR")
  scp "$REPO_ROOT"/mgmt/folia-nexa-mgmt_*.snap "$MGMT_HOST:$MGMT_TMP_DIR/"
  "${SSH_T[@]}" "$MGMT_HOST" "sudo snap install $MGMT_TMP_DIR/folia-nexa-mgmt_*.snap --dangerous"
  "${SSH_PLAIN[@]}" "$MGMT_HOST" "rm -rf -- '$MGMT_TMP_DIR'"
fi

# -- node / folia-node-base --------------------------------------------

if [[ -z "$SKIP_NODE" ]]; then
  IFS=',' read -ra hosts <<< "$LXD_HOSTS"
  for host in "${hosts[@]}"; do
    echo "==> Refreshing folia-node-base on $host"
    NODE_SNAP="$(ls "$REPO_ROOT"/node/folia-nexa-node_*.snap | head -1)"
    NODE_TMP_DIR="$(make_remote_tmp_dir "$host")"
    REMOTE_TMP_DIRS+=("$host|$NODE_TMP_DIR")
    scp "$NODE_SNAP" "$host:$NODE_TMP_DIR/"
    scp "$REPO_ROOT/tools/refresh-node-base-image.sh" "$host:$NODE_TMP_DIR/"
    "${SSH_T[@]}" "$host" "sudo bash '$NODE_TMP_DIR/refresh-node-base-image.sh' '$LXD_PROJECT' '$NODE_TMP_DIR/$(basename "$NODE_SNAP")'"
    "${SSH_PLAIN[@]}" "$host" "rm -rf -- '$NODE_TMP_DIR'"
  done
fi

# -- portal -----------------------------------------------------------------

if [[ -z "$SKIP_PORTAL" ]]; then
  echo "==> Deploying portal to $PORTAL_HOST"
  PORTAL_TMP_DIR="$(make_remote_tmp_dir "$PORTAL_HOST")"
  REMOTE_TMP_DIRS+=("$PORTAL_HOST|$PORTAL_TMP_DIR")
  scp "$REPO_ROOT"/portal/folia-nexa-portal_*.snap "$PORTAL_HOST:$PORTAL_TMP_DIR/"
  "${SSH_T[@]}" "$PORTAL_HOST" "sudo snap install $PORTAL_TMP_DIR/folia-nexa-portal_*.snap --dangerous"
  "${SSH_PLAIN[@]}" "$PORTAL_HOST" "rm -rf -- '$PORTAL_TMP_DIR'"
fi

# -- proxy -----------------------------------------------------------------

if [[ -z "$SKIP_PROXY" ]]; then
  proceed="$ASSUME_YES"
  if [[ -z "$proceed" ]]; then
    read -r -p "Deploy to the LIVE proxy at $PROXY_HOST? This restarts it and disconnects any connected players. [y/N] " reply
    [[ "$reply" =~ ^[Yy] ]] && proceed=1
  fi
  if [[ -n "$proceed" ]]; then
    echo "==> Deploying proxy to $PROXY_HOST"
    PROXY_TMP_DIR="$(make_remote_tmp_dir "$PROXY_HOST")"
    REMOTE_TMP_DIRS+=("$PROXY_HOST|$PROXY_TMP_DIR")
    scp "$REPO_ROOT"/proxy/folia-nexa-proxy_*.snap "$PROXY_HOST:$PROXY_TMP_DIR/"
    "${SSH_T[@]}" "$PROXY_HOST" "sudo snap install $PROXY_TMP_DIR/folia-nexa-proxy_*.snap --dangerous"
    "${SSH_PLAIN[@]}" "$PROXY_HOST" "rm -rf -- '$PROXY_TMP_DIR'"
  else
    echo "==> Skipping proxy deploy"
  fi
fi

echo "==> Done."
