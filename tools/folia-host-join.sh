#!/usr/bin/env bash
#
# folia-host-join.sh — enroll this LXD host as a compute resource for a
# folia-nexa-mgmt cluster. See PLAN.md §4 for the design this implements.
#
# What it does, in order:
#   1. Confirms LXD is installed and initialized.
#   2. Enables the LXD remote API (core.https_address) if not already set.
#   3. Creates (or reuses) a quota-capped "folia" project, sized from
#      detected CPU/memory unless overridden. Also makes sure that
#      project's own "default" profile actually has a root disk + network
#      device — features.profiles=true (LXD's default) gives every new
#      project its own empty default profile, and without this step every
#      container launch into it (including mgmt's own scheduler) fails
#      with "No root device could be found."
#   4. Generates a fresh, single-use LXD trust token scoped to that project.
#   5. Calls POST /api/v1/hosts/enroll on folia-nexa-mgmt, authenticated with
#      the join token from `folia-nexa-mgmt hosts create-join-token`, handing
#      over the LXD trust token so mgmt can complete the mTLS handshake
#      itself (equivalent to mgmt running `lxc remote add` with that token).
#
# The mgmt-side API (step 5) is a documented contract (PLAN.md §10), not
# necessarily live yet. Steps 1-4 are fully functional standalone; use
# --skip-enroll to stop after step 4 and finish the trust exchange by hand.

set -euo pipefail

PROG="$(basename "$0")"

MGMT_URL=""
JOIN_TOKEN=""
NODE_NAME=""
NODE_ADDRESS=""
API_PORT="8443"
PROJECT="folia"
CPU_CORES=""
MEMORY_GB=""
MAX_CONTAINERS="20"
CA_CERT=""
INSECURE="false"
SKIP_ENROLL="false"
ASSUME_YES="false"

usage() {
  cat <<EOF
Usage: sudo $PROG --mgmt-url <url> --join-token <token> [options]

Required:
  --mgmt-url URL        Base URL of folia-nexa-mgmt, e.g. https://mgmt.internal:8443
  --join-token TOKEN     One-time token from 'folia-nexa-mgmt hosts create-join-token'
                          (not required with --skip-enroll)

Options:
  --name NAME            Host name to register as (default: this host's short hostname)
  --address ADDR         IP/hostname mgmt should reach this host's LXD API on
                          (default: auto-detected primary IP — verify it's the
                          right interface on multi-homed hosts)
  --api-port PORT        LXD remote API port (default: 8443)
  --project NAME         LXD project to create/use for folia workloads (default: folia)
  --cpu N                CPU core quota for the project (default: detected via nproc)
  --memory GB            Memory quota in GB for the project (default: detected, minus
                          2GB headroom for the host kernel/ZFS ARC)
  --max-containers N     Container count quota for the project (default: 20)
  --ca-cert PATH         CA certificate to verify mgmt's TLS cert against
  --insecure              Skip TLS verification when calling mgmt (dev/self-signed only)
  --skip-enroll           Do everything except call mgmt; print the LXD trust token
                          instead, for manual completion or dry-running this script
  -y, --yes               Don't prompt for confirmation before applying quotas
  -h, --help               Show this help
EOF
}

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mgmt-url) MGMT_URL="$2"; shift 2 ;;
    --join-token) JOIN_TOKEN="$2"; shift 2 ;;
    --name) NODE_NAME="$2"; shift 2 ;;
    --address) NODE_ADDRESS="$2"; shift 2 ;;
    --api-port) API_PORT="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --cpu) CPU_CORES="$2"; shift 2 ;;
    --memory) MEMORY_GB="$2"; shift 2 ;;
    --max-containers) MAX_CONTAINERS="$2"; shift 2 ;;
    --ca-cert) CA_CERT="$2"; shift 2 ;;
    --insecure) INSECURE="true"; shift ;;
    --skip-enroll) SKIP_ENROLL="true"; shift ;;
    -y|--yes) ASSUME_YES="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1 (see --help)" ;;
  esac
done

[[ -z "$MGMT_URL" ]] && die "--mgmt-url is required (see --help)"
if [[ "$SKIP_ENROLL" == "false" && -z "$JOIN_TOKEN" ]]; then
  die "--join-token is required unless --skip-enroll is set (see --help)"
fi

command -v lxc >/dev/null 2>&1 || die "lxc not found — install/snap-install LXD first"
if [[ "$SKIP_ENROLL" == "false" ]]; then
  command -v curl >/dev/null 2>&1 || die "curl not found — required to call mgmt"
fi

if [[ $EUID -ne 0 ]] && ! id -nG "$USER" 2>/dev/null | grep -qw lxd; then
  die "run as root, or as a user in the 'lxd' group (needed for 'lxc config'/'lxc project')"
fi

# --- Step 1: LXD present and initialized -----------------------------------

lxc info >/dev/null 2>&1 || die "LXD doesn't appear to be initialized — run 'lxd init' first (storage pool choice is a decision this script won't make for you)"

# --- Node identity -----------------------------------------------------------

if [[ -z "$NODE_NAME" ]]; then
  NODE_NAME="$(hostname -s)"
  log "No --name given, using hostname: $NODE_NAME"
fi

if [[ -z "$NODE_ADDRESS" ]]; then
  NODE_ADDRESS="$(ip -4 route get 1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' | head -n1)"
  [[ -z "$NODE_ADDRESS" ]] && die "couldn't auto-detect this host's address — pass --address explicitly"
  warn "No --address given, auto-detected: $NODE_ADDRESS (verify this is the interface mgmt should reach — override with --address if not)"
fi

# --- Step 2: enable the LXD remote API --------------------------------------

CURRENT_HTTPS_ADDR="$(lxc config get core.https_address || true)"
if [[ -z "$CURRENT_HTTPS_ADDR" ]]; then
  log "Enabling LXD remote API on :$API_PORT"
  lxc config set core.https_address ":${API_PORT}"
else
  log "LXD remote API already enabled ($CURRENT_HTTPS_ADDR), leaving as-is"
fi

# --- Step 3: ensure the folia project exists with quotas --------------------

if lxc project list -f csv 2>/dev/null | cut -d',' -f1 | grep -qx "$PROJECT"; then
  log "Project '$PROJECT' already exists, reusing it"
else
  if [[ -z "$CPU_CORES" ]]; then
    CPU_CORES="$(nproc)"
  fi
  if [[ -z "$MEMORY_GB" ]]; then
    TOTAL_MEM_GB="$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)"
    MEMORY_GB=$(( TOTAL_MEM_GB > 2 ? TOTAL_MEM_GB - 2 : TOTAL_MEM_GB ))
    [[ "$MEMORY_GB" -lt "$TOTAL_MEM_GB" ]] && warn "reserving 2GB of ${TOTAL_MEM_GB}GB detected for host kernel/ZFS ARC headroom"
  fi

  log "About to create project '$PROJECT' with quota: ${CPU_CORES} CPU cores, ${MEMORY_GB}GB memory, ${MAX_CONTAINERS} containers"
  if [[ "$ASSUME_YES" != "true" ]]; then
    read -r -p "Proceed? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "aborted"
  fi

  lxc project create "$PROJECT" \
    -c "limits.cpu=${CPU_CORES}" \
    -c "limits.memory=${MEMORY_GB}GB" \
    -c "limits.containers=${MAX_CONTAINERS}" \
    -c "restricted=true" \
    -c "restricted.containers.nesting=block"
  log "Project '$PROJECT' created"
fi

# --- Step 3b: ensure the project's own default profile is actually usable ---
#
# A project with features.profiles=true (LXD's default) gets its own
# "default" profile, decoupled from the host's real default profile — and
# it starts out with zero devices. Left alone, every container launch in
# this project fails with "No root device could be found": both this
# script's own smoke-test launches and, more importantly, mgmt's
# scheduler (LXDClient.launch_container, which always passes
# profiles=["default"]) — so world placement is silently broken on this
# host until this runs, project-created-just-now or reused-from-before.
# Mirror whatever pool/network the host's own real default profile uses,
# so this doesn't hardcode a name that's wrong for hosts set up
# differently (falls back to LXD's own conventional defaults if that
# lookup comes up empty, e.g. a host with no "default" project profile).
if ! lxc profile show default --project "$PROJECT" 2>/dev/null | grep -q "^  root:"; then
  SRC_POOL="$(lxc profile show default --project default 2>/dev/null | awk '/pool:/{print $2; exit}')"
  [[ -z "$SRC_POOL" ]] && SRC_POOL="default"
  log "Project '$PROJECT''s default profile has no root disk device — adding one (pool: $SRC_POOL)"
  lxc profile device add default root disk path=/ pool="$SRC_POOL" --project "$PROJECT"
fi
if ! lxc profile show default --project "$PROJECT" 2>/dev/null | grep -q "type: nic"; then
  SRC_NETWORK="$(lxc profile show default --project default 2>/dev/null | awk '/network:/{print $2; exit}')"
  [[ -z "$SRC_NETWORK" ]] && SRC_NETWORK="lxdbr0"
  log "Project '$PROJECT''s default profile has no network device — adding one (network: $SRC_NETWORK)"
  lxc profile device add default eth0 nic network="$SRC_NETWORK" --project "$PROJECT"
fi

# --- Step 4: generate a fresh LXD trust token -------------------------------

TRUST_NAME="folia-nexa-mgmt-enroll-$(date +%s)"
log "Generating LXD trust token ('$TRUST_NAME', scoped to project '$PROJECT')"

TRUST_OUTPUT="$(lxc config trust add --name "$TRUST_NAME" --projects "$PROJECT" --restricted --quiet 2>&1)" \
  || die "failed to generate a trust token — check your LXD version supports 'lxc config trust add' token mode:\n$TRUST_OUTPUT"

# The exact wrapper text around the token varies by LXD version; pull the
# token itself (a long base64/hex-ish string with no whitespace) out of
# whatever was printed.
LXD_TRUST_TOKEN="$(printf '%s\n' "$TRUST_OUTPUT" | grep -oE '[A-Za-z0-9_=+./-]{40,}' | head -n1)"
[[ -z "$LXD_TRUST_TOKEN" ]] && die "couldn't parse a trust token out of LXD's output:\n$TRUST_OUTPUT"

log "Trust token generated (single-use, consumed on first connection)"

if [[ "$SKIP_ENROLL" == "true" ]]; then
  cat <<EOF

--skip-enroll set — stopping here. To finish manually from folia-nexa-mgmt:

  folia-nexa-mgmt hosts add ${NODE_NAME} \\
    --address ${NODE_ADDRESS}:${API_PORT} \\
    --project ${PROJECT} \\
    --trust-token ${LXD_TRUST_TOKEN}

EOF
  exit 0
fi

# --- Step 5: enroll with mgmt ------------------------------------------------

log "Calling ${MGMT_URL%/}/api/v1/hosts/enroll"

CURL_ARGS=(-sS -w '\n%{http_code}' -X POST "${MGMT_URL%/}/api/v1/hosts/enroll"
  -H "Authorization: Bearer ${JOIN_TOKEN}"
  -H "Content-Type: application/json"
  --data-binary @-)

[[ -n "$CA_CERT" ]] && CURL_ARGS=(--cacert "$CA_CERT" "${CURL_ARGS[@]}")
[[ "$INSECURE" == "true" ]] && { CURL_ARGS=(-k "${CURL_ARGS[@]}"); warn "TLS verification disabled (--insecure) — do not use this against a production mgmt endpoint"; }

PAYLOAD=$(cat <<JSON
{
  "name": "${NODE_NAME}",
  "address": "${NODE_ADDRESS}:${API_PORT}",
  "project": "${PROJECT}",
  "lxd_trust_token": "${LXD_TRUST_TOKEN}",
  "capacity": {
    "cpu_cores": ${CPU_CORES:-$(nproc)},
    "memory_gb": ${MEMORY_GB:-$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)}
  }
}
JSON
)

RESPONSE="$(printf '%s' "$PAYLOAD" | curl "${CURL_ARGS[@]}")" || die "request to mgmt failed (network error) — is ${MGMT_URL} reachable?"
HTTP_CODE="$(printf '%s' "$RESPONSE" | tail -n1)"
BODY="$(printf '%s' "$RESPONSE" | sed '$d')"

case "$HTTP_CODE" in
  200|201)
    log "Enrolled successfully as '${NODE_NAME}'"
    printf '%s\n' "$BODY"
    ;;
  401|403)
    die "mgmt rejected the join token (expired, already used, or invalid): $BODY"
    ;;
  409)
    die "mgmt already has a host with this name/address enrolled: $BODY"
    ;;
  "")
    die "no response from mgmt — check --mgmt-url, network path, and TLS trust (--ca-cert/--insecure)"
    ;;
  *)
    die "mgmt returned HTTP ${HTTP_CODE}: $BODY"
    ;;
esac
