#!/usr/bin/env bash
#
# setup-wireguard.sh — generate this peer's WireGuard keypair and a
# filled-in wg0.conf for the VPS<->home tunnel mesh described in
# docs/vps-edge-deployment.md (PLAN.md §7A, §7C). Run once per home LXD
# host you want reachable from the VPS, plus once on the VPS itself per
# home host you add.
#
# Never transmits a private key anywhere — generates locally with `wg
# genkey`, writes it to a root-only file, and only ever prints/asks you to
# paste the *public* key. Mirrors tools/folia-host-join.sh's handling of
# LXD trust tokens: the secret stays on the box that generated it.
#
# --role home is always exactly one peer (the VPS) — a home LXD host talks
# to the mesh through a single tunnel, so this side is unchanged from a
# single-peer setup: --peer-name only affects log output there, not the
# file layout.
#
# --role vps is a mesh of N peers, one per home LXD host, added
# incrementally without disturbing the others already configured: each
# peer's [Peer] stanza is kept in its own file under
# $(dirname OUTPUT)/peers.d/<peer-name>.conf, and wg0.conf itself is always
# just [Interface] + every file in peers.d/ concatenated together. Re-run
# with the same --peer-name to update one home host's entry (its old
# stanza is fully replaced); a new --peer-name adds a new home host without
# touching anyone else's.
#
# Typical flow, for a first home host named "node-a":
#   1. On the VPS:  sudo ./setup-wireguard.sh --role vps --peer-name node-a
#      (prints the VPS's public key, since --peer-public-key isn't known yet —
#      the VPS's own keypair is generated once and reused for every peer)
#   2. On node-a:  sudo ./setup-wireguard.sh --role home \
#        --peer-public-key <vps-pubkey-from-step-1> \
#        --peer-endpoint <vps-public-ip>:51820
#      (writes node-a's full wg0.conf, and prints node-a's own public key)
#   3. Back on the VPS:  sudo ./setup-wireguard.sh --role vps --peer-name node-a \
#        --peer-public-key <node-a-pubkey-from-step-2> \
#        --allowed-ips <node-a-tunnel-ip>/32,<node-a-lan-subnet>
#      (writes/updates just node-a's peers.d entry and regenerates wg0.conf)
#   4. On each peer:  systemctl enable --now wg-quick@wg0
#
# To add a second home host "node-b" later: repeat steps 1-3 with
# --peer-name node-b and a distinct --address on node-b's own --role home
# run (e.g. 10.66.0.3/24) — node-a's peers.d entry is untouched, and a
# `wg syncconf` (see the end of this script's output) picks it up on the
# VPS without dropping node-a's already-connected session.
#
# This script does NOT touch firewall/nftables rules, IP forwarding, or the
# LXD/mgmt-facing AllowedIPs beyond the placeholder default below — those
# are host-specific decisions covered in docs/vps-edge-deployment.md, not
# something this script should guess at.

set -euo pipefail

PROG="$(basename "$0")"

ROLE=""
ADDRESS=""
LISTEN_PORT="51820"
PEER_NAME=""
PEER_PUBLIC_KEY=""
PEER_ENDPOINT=""
ALLOWED_IPS=""
OUTPUT="/etc/wireguard/wg0.conf"
ASSUME_YES="false"

usage() {
  cat <<EOF
Usage: sudo $PROG --role vps|home [options]

Required:
  --role vps|home          Which peer this is

Options:
  --address CIDR            This peer's wg0 address (default: 10.66.0.1/24
                              for --role vps, 10.66.0.2/24 for --role home —
                              give each home host on the mesh its own
                              distinct address, e.g. 10.66.0.3/24 for a
                              second one)
  --listen-port PORT        UDP port WireGuard listens on (default: 51820)
  --peer-name NAME           --role vps only: identifies which home host
                              this invocation adds/updates (default:
                              "default" — fine for a single-home-host setup,
                              but pick a real name, e.g. the LXD host's own
                              name, once you have more than one). Each name
                              gets its own file under peers.d/ next to
                              --output, so re-running for one home host
                              never disturbs any other already-configured
                              peer. Ignored (only affects log text) for
                              --role home, which always has exactly one peer.
  --peer-public-key KEY      The other peer's public WireGuard key. Omit on
                              the first run per peer — this script will
                              print your own public key to hand to the other
                              side instead, then re-run with this flag once
                              you have theirs.
  --peer-endpoint HOST:PORT  Required for --role home: the VPS's public
                              IP/hostname and port (e.g. 203.0.113.5:51820).
                              The VPS never needs the home peer's endpoint —
                              home always dials out, so there's no inbound
                              port to forward at home.
  --allowed-ips CIDR[,CIDR,...]  What this peer routes to the other peer
                              over the tunnel. Defaults: --role home routes
                              to the VPS's tunnel address (10.66.0.1/32)
                              plus the loopback the relocated proxy needs to
                              reach it on. --role vps defaults to
                              10.66.0.2/32 only for the implicit single-peer
                              case (--peer-name unset/"default"); with a
                              real --peer-name this is required — there's no
                              safe way to guess one home host's tunnel
                              address among several, so pass at minimum its
                              tunnel /32 (e.g. 10.66.0.3/32), plus mgmt's and
                              that host's LXD bridge subnet once you know
                              them.
  --output PATH              Where to write the config (default:
                              /etc/wireguard/wg0.conf). --role vps also
                              keeps per-peer files under peers.d/ next to
                              this path.
  -y, --yes                  Don't prompt before overwriting an existing
                              peer entry (--role vps) or config (--role home)
  -h, --help                  Show this help
EOF
}

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --address) ADDRESS="$2"; shift 2 ;;
    --listen-port) LISTEN_PORT="$2"; shift 2 ;;
    --peer-name) PEER_NAME="$2"; shift 2 ;;
    --peer-public-key) PEER_PUBLIC_KEY="$2"; shift 2 ;;
    --peer-endpoint) PEER_ENDPOINT="$2"; shift 2 ;;
    --allowed-ips) ALLOWED_IPS="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    -y|--yes) ASSUME_YES="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1 (see --help)" ;;
  esac
done

[[ "$ROLE" == "vps" || "$ROLE" == "home" ]] || die "--role must be 'vps' or 'home' (see --help)"
command -v wg >/dev/null 2>&1 || die "wg not found — install wireguard-tools first"
[[ $EUID -eq 0 ]] || die "run as root (writes to /etc/wireguard)"

if [[ "$ROLE" == "home" && -z "$PEER_ENDPOINT" ]]; then
  die "--peer-endpoint is required for --role home (the VPS's public IP:port) — see --help"
fi

if [[ -z "$PEER_NAME" ]]; then
  PEER_NAME="default"
fi

if [[ -z "$ADDRESS" ]]; then
  if [[ "$ROLE" == "vps" ]]; then ADDRESS="10.66.0.1/24"; else ADDRESS="10.66.0.2/24"; fi
  log "No --address given, using default for --role $ROLE: $ADDRESS"
fi

if [[ -z "$ALLOWED_IPS" ]]; then
  if [[ "$ROLE" == "vps" ]]; then
    if [[ "$PEER_NAME" != "default" ]]; then
      die "--allowed-ips is required for --role vps with --peer-name '$PEER_NAME' — there's no safe way to guess this home host's tunnel address among several (see --help)"
    fi
    ALLOWED_IPS="10.66.0.2/32"
    warn "No --allowed-ips given — defaulting to just the home peer's tunnel address (10.66.0.2/32)." \
         "Add mgmt's and your LXD hosts' real LAN addresses once you know them (see docs/vps-edge-deployment.md)," \
         "or nothing on the home side beyond the tunnel itself will be reachable from the VPS."
  else
    ALLOWED_IPS="10.66.0.1/32"
  fi
  log "Using default --allowed-ips for --role $ROLE: $ALLOWED_IPS"
fi

KEY_DIR="$(dirname "$OUTPUT")"
mkdir -p "$KEY_DIR"
PRIVATE_KEY_FILE="$KEY_DIR/wg0-private.key"

if [[ -f "$PRIVATE_KEY_FILE" ]]; then
  log "Reusing existing private key at $PRIVATE_KEY_FILE (delete it first if you really want a new keypair — that would orphan the public key you already handed to the other peer)"
else
  log "Generating a new WireGuard keypair"
  umask 077
  wg genkey > "$PRIVATE_KEY_FILE"
  chmod 600 "$PRIVATE_KEY_FILE"
fi

PRIVATE_KEY="$(cat "$PRIVATE_KEY_FILE")"
PUBLIC_KEY="$(wg pubkey < "$PRIVATE_KEY_FILE")"

if [[ -z "$PEER_PUBLIC_KEY" ]]; then
  cat <<EOF

No --peer-public-key given yet — stopping before writing $OUTPUT.

This peer's public key (safe to share, hand it to the other side):

  $PUBLIC_KEY

Run this script on the OTHER peer now (if you haven't already), then
re-run this command with:

  --peer-public-key <the-other-peers-public-key>

EOF
  exit 0
fi

if [[ "$ROLE" == "home" ]]; then
  if [[ -f "$OUTPUT" && "$ASSUME_YES" != "true" ]]; then
    read -r -p "$OUTPUT already exists — overwrite? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "aborted"
  fi

  {
    echo "[Interface]"
    echo "Address = ${ADDRESS}"
    echo "PrivateKey = ${PRIVATE_KEY}"
    echo "ListenPort = ${LISTEN_PORT}"
    echo ""
    echo "[Peer]"
    echo "PublicKey = ${PEER_PUBLIC_KEY}"
    echo "AllowedIPs = ${ALLOWED_IPS}"
    echo "Endpoint = ${PEER_ENDPOINT}"
    # Home is always the one behind NAT with no forwarded port, so it must
    # be the side that dials out and keeps the mapping alive — see
    # docs/vps-edge-deployment.md for why this is the crux of "no port
    # forwarding needed at home".
    echo "PersistentKeepalive = 25"
  } > "$OUTPUT"
  chmod 600 "$OUTPUT"

  log "Wrote $OUTPUT"
  log "This peer's public key (unchanged, printed again for convenience): $PUBLIC_KEY"
  log "Next: review $OUTPUT, then 'systemctl enable --now wg-quick@wg0' to bring the tunnel up."
else
  # --role vps: one [Peer] stanza per home host, kept in its own file so
  # adding/updating one never disturbs any other already-configured peer.
  PEERS_DIR="$KEY_DIR/peers.d"
  mkdir -p "$PEERS_DIR"
  chmod 700 "$PEERS_DIR"
  PEER_FILE="$PEERS_DIR/${PEER_NAME}.conf"

  if [[ -f "$PEER_FILE" && "$ASSUME_YES" != "true" ]]; then
    read -r -p "Peer '$PEER_NAME' is already configured at $PEER_FILE — replace it? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "aborted"
  fi

  {
    echo "[Peer]"
    echo "# peer: ${PEER_NAME}"
    echo "PublicKey = ${PEER_PUBLIC_KEY}"
    echo "AllowedIPs = ${ALLOWED_IPS}"
  } > "$PEER_FILE"
  chmod 600 "$PEER_FILE"

  {
    echo "[Interface]"
    echo "Address = ${ADDRESS}"
    echo "PrivateKey = ${PRIVATE_KEY}"
    echo "ListenPort = ${LISTEN_PORT}"
    for f in "$PEERS_DIR"/*.conf; do
      echo ""
      cat "$f"
    done
  } > "$OUTPUT"
  chmod 600 "$OUTPUT"

  PEER_COUNT=$(find "$PEERS_DIR" -maxdepth 1 -name '*.conf' | wc -l)
  log "Wrote peer '$PEER_NAME' to $PEER_FILE and regenerated $OUTPUT ($PEER_COUNT peer(s) total)"
  log "This peer's public key (unchanged, printed again for convenience): $PUBLIC_KEY"
  log "Next: review $OUTPUT, then 'systemctl enable --now wg-quick@wg0' the first time, or"
  log "  'wg syncconf wg0 <(wg-quick strip wg0)' afterward to hot-apply without dropping other peers' sessions."
fi

if [[ "$ROLE" == "vps" ]]; then
  log "Remember to open UDP ${LISTEN_PORT} in the VPS's firewall (e.g. 'ufw allow ${LISTEN_PORT}/udp')."
fi
