#!/usr/bin/env bash
#
# migrate-storage-to-zfs.sh — create a ZFS-backed LXD storage pool and
# migrate every container in a project (default: "folia") onto it.
#
# Why: LXD's "dir" storage driver has no native copy-on-write snapshot
# support — every LXD instance snapshot on a dir-backed pool freezes the
# whole container for the duration of a full rootfs rsync copy, which on
# a live Minecraft world (folia-nexa-node) means a real, potentially
# multi-minute player-visible outage on every backup, scheduled or
# manual. Confirmed live (2026-08-24): a manual backup wedged a
# container in FREEZING after a client-side timeout caused overlapping
# snapshot requests to pile up against the same instance. ZFS snapshots
# are metadata-only copy-on-write — no freeze at all — which fixes this
# at the root instead of working around it in mgmt's own code. See
# CLAUDE.md's World backups entry for the full incident history.
#
# What it does, in order:
#   1. Creates (or reuses) a ZFS-backed LXD storage pool.
#   2. Points the project's own "default" profile's root disk device at
#      that pool, so every *future* container launch (mgmt's scheduler
#      included) lands on it automatically.
#   3. For every existing container in the project not already on that
#      pool: stops it (if running), moves its root disk to the new pool
#      (`lxc move ... --storage`), and restarts it (if it was running
#      before). Already-migrated containers are skipped, so re-running
#      this script after an interruption is safe.
#
# Every `lxc` call this script makes against instances or profiles is
# scoped with --project — it never lists, stops, moves, or starts a
# container outside the target project, even on a host running other
# unrelated LXD projects alongside it.
#
# NOTE: written against LXD's documented `storage create`/`move` CLI
# contract, not yet exercised against a live LXD daemon in this
# environment (same caveat as this repo's other tools/*.sh scripts —
# see CLAUDE.md). Try --only against one, non-critical container first
# before migrating a whole live cluster.
#
# The OLD storage pool is left in place, untouched, once migration
# finishes — remove it yourself once you've confirmed everything is
# healthy on the new one:
#   lxc storage volume list <old-pool>   # confirm nothing's left on it
#   lxc storage delete <old-pool>

set -euo pipefail

PROG="$(basename "$0")"

PROJECT="folia"
POOL_NAME="folia-zfs"
SOURCE=""
SIZE=""
ONLY=""
ASSUME_YES="false"

usage() {
  cat <<EOF
Usage: sudo $PROG [options]

Options:
  --project NAME     LXD project whose containers to migrate (default: folia)
  --pool NAME        Name for the new ZFS storage pool (default: folia-zfs)
  --source PATH      Block device (e.g. /dev/sdb) or existing zpool/dataset
                      to back the new pool with. DESTRUCTIVE if a raw block
                      device — it will be wiped. Omit for a loop-file-backed
                      pool instead (fine for testing, not recommended for a
                      real production world host — see --size).
  --size SIZE        Size for a loop-file-backed pool when --source is
                      omitted, e.g. 100GB (LXD picks its own default —
                      a fraction of free host disk space — if unset)
  --only NAME        Migrate just this one container, not the whole
                      project (recommended for a first, cautious run)
  -y, --yes          Don't prompt for confirmation before destructive steps
  -h, --help         Show this help
EOF
}

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --pool) POOL_NAME="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --size) SIZE="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    -y|--yes) ASSUME_YES="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1 (see --help)" ;;
  esac
done

command -v lxc >/dev/null 2>&1 || die "lxc not found — install/snap-install LXD first"
command -v zfs >/dev/null 2>&1 || die "zfs userspace tools not found — install zfsutils-linux first (LXD's zfs driver needs it on the host even though LXD manages the pool itself)"

if [[ $EUID -ne 0 ]] && ! id -nG "$USER" 2>/dev/null | grep -qw lxd; then
  die "run as root, or as a user in the 'lxd' group (needed for 'lxc storage'/'lxc move')"
fi

lxc project list -f csv 2>/dev/null | cut -d',' -f1 | grep -qx "$PROJECT" \
  || die "no such LXD project '$PROJECT' — nothing to migrate"

confirm() {
  [[ "$ASSUME_YES" == "true" ]] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

# --- Step 1: create (or reuse) the ZFS storage pool -------------------------

if lxc storage list -f csv 2>/dev/null | cut -d',' -f1 | grep -qx "$POOL_NAME"; then
  ACTUAL_DRIVER="$(lxc storage list -f csv 2>/dev/null | awk -F',' -v p="$POOL_NAME" '$1==p{print $2}')"
  [[ "$ACTUAL_DRIVER" == "zfs" ]] || die "storage pool '$POOL_NAME' already exists but uses driver '$ACTUAL_DRIVER', not zfs — pick a different --pool name"
  log "Storage pool '$POOL_NAME' already exists (zfs), reusing it"
else
  STORAGE_CREATE_ARGS=("$POOL_NAME" zfs)
  if [[ -n "$SOURCE" ]]; then
    if [[ -b "$SOURCE" ]]; then
      warn "'$SOURCE' is a block device — creating this pool will WIPE it entirely"
      confirm "Create ZFS pool '$POOL_NAME' on $SOURCE, destroying any existing data there?" || die "aborted"
    else
      log "Using '$SOURCE' as an existing zpool/dataset for '$POOL_NAME'"
    fi
    STORAGE_CREATE_ARGS+=("source=$SOURCE")
  else
    warn "No --source given — creating a loop-file-backed pool (fine for testing; a dedicated block device is recommended for a real world host)"
    [[ -n "$SIZE" ]] && STORAGE_CREATE_ARGS+=("size=$SIZE")
    confirm "Create loop-file-backed ZFS pool '$POOL_NAME'${SIZE:+ ($SIZE)}?" || die "aborted"
  fi
  log "Creating ZFS storage pool '$POOL_NAME'"
  lxc storage create "${STORAGE_CREATE_ARGS[@]}"
  log "Storage pool '$POOL_NAME' created"
fi

# --- Step 2: point the project's default profile at the new pool -----------
#
# So every *future* container launch (mgmt's scheduler included,
# LXDClient.launch_container always passes profiles=["default"]) lands on
# the new pool without needing any mgmt-side config change. Existing
# containers keep whatever root-disk device they already have (their own
# per-instance override once Step 3 moves them) — updating this shared
# profile doesn't retroactively move anything that's already running,
# which is exactly why Step 3 exists.

CURRENT_PROFILE_POOL="$(lxc profile show default --project "$PROJECT" 2>/dev/null | awk '/pool:/{print $2; exit}')"
if [[ "$CURRENT_PROFILE_POOL" != "$POOL_NAME" ]]; then
  log "Pointing project '$PROJECT''s default profile's root disk at '$POOL_NAME' (was: ${CURRENT_PROFILE_POOL:-none})"
  lxc profile device set default root pool="$POOL_NAME" --project "$PROJECT"
else
  log "Project '$PROJECT''s default profile already points at '$POOL_NAME'"
fi

# --- Step 3: migrate existing containers ------------------------------------

if [[ -n "$ONLY" ]]; then
  CANDIDATES="$ONLY"
else
  CANDIDATES="$(lxc list --project "$PROJECT" -f csv -c n 2>/dev/null)"
fi

if [[ -z "$CANDIDATES" ]]; then
  log "No containers in project '$PROJECT' — nothing to migrate"
  exit 0
fi

# First pass: figure out what actually needs moving (skip anything already
# on the target pool) before asking for one combined confirmation, rather
# than prompting once per container.
TO_MIGRATE=()
while IFS= read -r NAME; do
  [[ -z "$NAME" ]] && continue
  CURRENT_POOL="$(lxc config show "$NAME" --project "$PROJECT" --expanded 2>/dev/null | awk '/pool:/{print $2; exit}')"
  if [[ "$CURRENT_POOL" == "$POOL_NAME" ]]; then
    log "'$NAME' is already on '$POOL_NAME', skipping"
    continue
  fi
  TO_MIGRATE+=("$NAME")
done <<< "$CANDIDATES"

if [[ "${#TO_MIGRATE[@]}" -eq 0 ]]; then
  log "Every container in project '$PROJECT' is already on '$POOL_NAME' — nothing to migrate"
  exit 0
fi

log "About to migrate ${#TO_MIGRATE[@]} container(s) in project '$PROJECT' to '$POOL_NAME': ${TO_MIGRATE[*]}"
warn "each one will be briefly stopped and restarted — players on that world will be disconnected for the duration"
confirm "Proceed?" || die "aborted"

MIGRATED=0
FAILED=0

for NAME in "${TO_MIGRATE[@]}"; do
  CURRENT_POOL="$(lxc config show "$NAME" --project "$PROJECT" --expanded 2>/dev/null | awk '/pool:/{print $2; exit}')"
  STATUS="$(lxc list --project "$PROJECT" -f csv -c ns 2>/dev/null | awk -F',' -v n="$NAME" '$1==n{print $2}')"
  WAS_RUNNING="false"
  [[ "$STATUS" == "RUNNING" ]] && WAS_RUNNING="true"

  log "Migrating '$NAME' (${CURRENT_POOL:-unknown} -> $POOL_NAME)"

  if [[ "$WAS_RUNNING" == "true" ]]; then
    log "Stopping '$NAME'"
    if ! lxc stop "$NAME" --project "$PROJECT"; then
      warn "'$NAME' failed to stop cleanly — skipping this container, nothing was touched"
      FAILED=$((FAILED + 1))
      continue
    fi
  fi

  if lxc move "$NAME" "$NAME" --project "$PROJECT" --storage "$POOL_NAME"; then
    log "'$NAME' moved to '$POOL_NAME'"
    MIGRATED=$((MIGRATED + 1))
  else
    warn "'$NAME' failed to move to '$POOL_NAME' — leaving it as-is on '$CURRENT_POOL'"
    FAILED=$((FAILED + 1))
  fi

  if [[ "$WAS_RUNNING" == "true" ]]; then
    log "Starting '$NAME' back up"
    lxc start "$NAME" --project "$PROJECT" \
      || warn "'$NAME' didn't start cleanly after migration — check it by hand ('lxc info $NAME --project $PROJECT --show-log')"
  fi
done

log "Done: $MIGRATED migrated, $FAILED failed (of ${#TO_MIGRATE[@]} attempted)"

if [[ "$FAILED" -gt 0 ]]; then
  warn "one or more containers failed to migrate — re-run this script to retry just those (already-migrated containers are skipped automatically)"
  exit 1
fi

cat <<EOF

Migration complete. The old storage pool is untouched — once you've
confirmed every world is healthy on '$POOL_NAME', you can remove it
yourself:

  lxc storage volume list <old-pool>   # confirm nothing's left on it
  lxc storage delete <old-pool>

EOF
