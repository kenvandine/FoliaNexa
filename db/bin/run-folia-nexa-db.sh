#!/bin/bash
# folia-nexa-db's daemon entrypoint. Self-contained on purpose: first run
# initializes a MariaDB data directory under $SNAP_COMMON (persists
# across snap refreshes) and bootstraps a dedicated database + user with
# a randomly generated password, written to $SNAP_COMMON/credentials.env
# for the operator to read once and feed into folia-nexa-mgmt's
# luckperms_mysql_* settings. Every run after that just starts mariadbd.
#
# Deliberately its own snap, not bundled into folia-nexa-mgmt's — see
# PLAN.md §11B: a `snap refresh folia-nexa-mgmt` shouldn't drop every
# world's active LuckPerms connection, and this needs to be reachable
# from every world on every host, not just wherever mgmt happens to run.
#
# Run end-to-end against the real MariaDB 11.8 binaries (extracted from
# Ubuntu's .deb packages, outside snap confinement — no snapd/root access
# available for an actual `snap install` here): init, startup, bootstrap,
# a real client connection with the generated credentials, and confirming
# root has no wildcard/remote grant all worked. NOT verified under actual
# strict confinement, though — that could plausibly restrict something
# mariadbd wants that running the binary directly didn't exercise.
set -euo pipefail

DATA_DIR="$SNAP_COMMON/mysql"
SOCKET="$SNAP_COMMON/mysqld.sock"
CREDENTIALS_FILE="$SNAP_COMMON/credentials.env"
DB_NAME="${FOLIA_DB_NAME:-luckperms}"
DB_USER="${FOLIA_DB_USER:-luckperms}"
DB_PORT="${FOLIA_DB_PORT:-3306}"

log() { printf '==> %s\n' "$*"; }

FIRST_RUN=false
if [ ! -d "$DATA_DIR/mysql" ]; then
  FIRST_RUN=true
  log "First run: initializing data directory at $DATA_DIR"
  mkdir -p "$DATA_DIR"
  "$SNAP/usr/bin/mariadb-install-db" \
    --basedir="$SNAP/usr" \
    --datadir="$DATA_DIR" \
    --auth-root-authentication-method=normal \
    --skip-test-db \
    >/dev/null
fi

"$SNAP/usr/sbin/mariadbd" \
  --datadir="$DATA_DIR" \
  --socket="$SOCKET" \
  --bind-address=0.0.0.0 \
  --port="$DB_PORT" &
MARIADB_PID=$!

log "Waiting for MariaDB to accept connections"
for _ in $(seq 1 60); do
  "$SNAP/usr/bin/mysqladmin" --socket="$SOCKET" ping >/dev/null 2>&1 && break
  sleep 1
done

if [ "$FIRST_RUN" = true ]; then
  DB_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  log "Bootstrapping database '$DB_NAME' and user '$DB_USER'"
  "$SNAP/usr/bin/mysql" --socket="$SOCKET" -u root <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`;
CREATE USER IF NOT EXISTS '${DB_USER}'@'%' IDENTIFIED BY '${DB_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'%';
FLUSH PRIVILEGES;
SQL

  # root stays local-only (no root@'%' grant above) — reachable only via
  # this snap's own unix socket, never over the network. Best-effort
  # address autodetection for the credentials file; override by hand if
  # this host is multi-homed and picks the wrong interface.
  HOST_ADDR="$(ip -4 route get 1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' | head -n1)"

  cat > "$CREDENTIALS_FILE" <<EOF
# Generated on first start of folia-nexa-db. Feed these into folia-nexa-mgmt's
# environment (PLAN.md §11B) — see CLAUDE.md's LuckPerms bootstrap phase.
FOLIA_MGMT_LUCKPERMS_MYSQL_HOST=${HOST_ADDR:-<fill-in-this-hosts-address>}
FOLIA_MGMT_LUCKPERMS_MYSQL_PORT=${DB_PORT}
FOLIA_MGMT_LUCKPERMS_MYSQL_DATABASE=${DB_NAME}
FOLIA_MGMT_LUCKPERMS_MYSQL_USER=${DB_USER}
FOLIA_MGMT_LUCKPERMS_MYSQL_PASSWORD=${DB_PASSWORD}
EOF
  chmod 600 "$CREDENTIALS_FILE"
  log "Credentials written to $CREDENTIALS_FILE"
fi

wait "$MARIADB_PID"
