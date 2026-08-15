#!/usr/bin/env bash
#
# setup-wireguard.sh — generate this peer's WireGuard keypair and a
# filled-in wg0.conf for the VPS<->home tunnel described in
# docs/vps-edge-deployment.md (PLAN.md §7A). Run once on each of the two
# peers: the Linode VPS, and the home LXD host.
#
# Never transmits a private key anywhere — generates locally with `wg
# genkey`, writes it to a root-only file, and only ever prints/asks you to
# paste the *public* key. Mirrors tools/folia-host-join.sh's handling of
# LXD trust tokens: the secret stays on the box that generated it.
#
# Typical flow:
#   1. On the VPS:  sudo ./setup-wireguard.sh --role vps
#      (prints the VPS's public key, since --peer-public-key isn't known yet)
#   2. On the home host:  sudo ./setup-wireguard.sh --role home \
#        --peer-public-key <vps-pubkey-from-step-1> \
#        --peer-endpoint <vps-public-ip>:51820
#      (writes home's full wg0.conf, and prints home's own public key)
#   3. Back on the VPS:  sudo ./setup-wireguard.sh --role vps \
#        --peer-public-key <home-pubkey-from-step-2>
#      (writes the VPS's full wg0.conf)
#   4. On each peer:  systemctl enable --now wg-quick@wg0
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
                              for --role vps, 10.66.0.2/24 for --role home)
  --listen-port PORT        UDP port WireGuard listens on (default: 51820)
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
                              over the tunnel. Defaults: --role vps routes
                              only to the home peer's own tunnel address
                              (10.66.0.2/32) until you add mgmt's and your
                              LXD hosts' real LAN addresses once you know
                              them; --role home routes to the VPS's tunnel
                              address (10.66.0.1/32) plus the loopback the
                              relocated proxy needs to reach it on.
  --output PATH              Where to write the config (default:
                              /etc/wireguard/wg0.conf)
  -y, --yes                  Don't prompt before overwriting an existing
                              config
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

if [[ -z "$ADDRESS" ]]; then
  if [[ "$ROLE" == "vps" ]]; then ADDRESS="10.66.0.1/24"; else ADDRESS="10.66.0.2/24"; fi
  log "No --address given, using default for --role $ROLE: $ADDRESS"
fi

if [[ -z "$ALLOWED_IPS" ]]; then
  if [[ "$ROLE" == "vps" ]]; then
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
  if [[ "$ROLE" == "home" ]]; then
    echo "Endpoint = ${PEER_ENDPOINT}"
    # Home is always the one behind NAT with no forwarded port, so it must
    # be the side that dials out and keeps the mapping alive — see
    # docs/vps-edge-deployment.md for why this is the crux of "no port
    # forwarding needed at home".
    echo "PersistentKeepalive = 25"
  fi
} > "$OUTPUT"
chmod 600 "$OUTPUT"

log "Wrote $OUTPUT"
log "This peer's public key (unchanged, printed again for convenience): $PUBLIC_KEY"
log "Next: review $OUTPUT, then 'systemctl enable --now wg-quick@wg0' to bring the tunnel up."
if [[ "$ROLE" == "vps" ]]; then
  log "Remember to open UDP ${LISTEN_PORT} in the VPS's firewall (e.g. 'ufw allow ${LISTEN_PORT}/udp')."
fi
