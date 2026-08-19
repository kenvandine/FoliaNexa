#!/usr/bin/env bash
#
# build-snap.sh — wraps `snapcraft` for one of this repo's six components
# (mgmt, node, proxy, bot, db, portal) so its snap's version string carries
# the exact commit it was built from.
#
# Why this exists: each component's snapcraft.yaml uses `source: .` so a
# real `snapcraft` build (this repo's whole verification story, see
# CLAUDE.md) picks up local/uncommitted work, not just the last commit.
# But that also means the LXD/multipass build instance snapcraft launches
# only ever receives a plain copy of that one subdirectory — confirmed for
# real: neither `source: ..` (a local copy) nor `source: .. /
# source-type: git` (an explicit clone) can see anything above it, because
# the project directory is copied into the build instance standalone, not
# bind-mounted alongside the rest of the working tree. `git -C` from
# inside the build environment therefore has no `.git` to find, at any
# depth. So the commit hash has to be computed out here, on the host,
# before snapcraft ever starts, and handed to the build the only way that
# survives the copy: as a real file inside the component directory itself.
#
# Usage: tools/build-snap.sh <mgmt|node|proxy|bot|db|portal> [snapcraft args...]
#
# Each component's snapcraft.yaml reads the .build-commit file this writes
# via adopt-info + override-build/override-pull (falls back to a plain
# base version, no hash, if that file isn't there — so a bare `snapcraft`
# run directly in a component directory, bypassing this wrapper, still
# works, just without a commit in the version string).

set -euo pipefail

DIR="${1:?usage: $0 <mgmt|node|proxy|bot|db|portal> [snapcraft args...]}"
shift

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPONENT_DIR="$REPO_ROOT/$DIR"

if [ ! -f "$COMPONENT_DIR/snapcraft.yaml" ]; then
  echo "error: $COMPONENT_DIR/snapcraft.yaml not found — is '$DIR' one of mgmt/node/proxy/bot/db/portal?" >&2
  exit 1
fi

HASH="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  HASH="${HASH}-dirty"
fi

echo "$HASH" > "$COMPONENT_DIR/.build-commit"
cleanup() { rm -f "$COMPONENT_DIR/.build-commit"; }
trap cleanup EXIT

echo "--- building $DIR from commit $HASH"
(cd "$COMPONENT_DIR" && snapcraft "$@")
