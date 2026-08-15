#!/usr/bin/env bash
# Stands up a plain LXD container running MariaDB to back LuckPerms
# across every world (PLAN.md §11B). Run this ONCE, directly on any
# trusted LXD host, before pointing folia-nexa-mgmt's luckperms_mysql_*
# settings at it — it is NOT run through mgmt's scheduler, because
# folia-nexa-node only knows how to run a Folia/Paper JVM, not arbitrary
# services. See mgmt/src/folia_mgmt/luckperms.py's module docstring for
# the full reasoning.
#
# NOT exercised against a live LXD daemon in this environment — same
# caveat as tools/folia-host-join.sh and everything else here that
# touches `lxc`.
set -euo pipefail

CONTAINER_NAME="${1:-luckperms-mysql}"
PROJECT="${FOLIA_LXD_PROJECT:-folia}"
DB_NAME="${LUCKPERMS_DB_NAME:-luckperms}"
DB_USER="${LUCKPERMS_DB_USER:-luckperms}"
DB_PASSWORD="${LUCKPERMS_DB_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"

log() { printf '==> %s\n' "$*"; }

log "Launching '$CONTAINER_NAME' in project '$PROJECT'"
lxc launch ubuntu:24.04 "$CONTAINER_NAME" --project "$PROJECT"

log "Waiting for network..."
for _ in $(seq 1 30); do
  lxc exec "$CONTAINER_NAME" --project "$PROJECT" -- true 2>/dev/null && break
  sleep 2
done

log "Installing mariadb-server"
lxc exec "$CONTAINER_NAME" --project "$PROJECT" -- bash -c "
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq mariadb-server
"

log "Allowing connections from the folia bridge network (edit /etc/mysql/mariadb.conf.d/50-server.cnf's bind-address by hand for anything beyond same-host testing)"
lxc exec "$CONTAINER_NAME" --project "$PROJECT" -- bash -c "
  set -e
  sed -i 's/^bind-address.*/bind-address = 0.0.0.0/' /etc/mysql/mariadb.conf.d/50-server.cnf
  systemctl restart mariadb
"

log "Creating database and user"
lxc exec "$CONTAINER_NAME" --project "$PROJECT" -- mysql -e "
  CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`;
  CREATE USER IF NOT EXISTS '${DB_USER}'@'%' IDENTIFIED BY '${DB_PASSWORD}';
  GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'%';
  FLUSH PRIVILEGES;
"

CONTAINER_IP=$(lxc list "$CONTAINER_NAME" --project "$PROJECT" -f csv -c 4 | head -n1 | cut -d' ' -f1)

cat <<EOF

==> Done. Point folia-nexa-mgmt at this instance:

  FOLIA_MGMT_LUCKPERMS_MYSQL_HOST=${CONTAINER_IP}
  FOLIA_MGMT_LUCKPERMS_MYSQL_PORT=3306
  FOLIA_MGMT_LUCKPERMS_MYSQL_DATABASE=${DB_NAME}
  FOLIA_MGMT_LUCKPERMS_MYSQL_USER=${DB_USER}
  FOLIA_MGMT_LUCKPERMS_MYSQL_PASSWORD=${DB_PASSWORD}

Once mgmt has these set, every running world with "LuckPerms" in its
plugin list gets its config.yml kept in sync with this database
automatically (see PLAN.md §11B) — no per-world manual config needed.

This container is unmanaged by mgmt's scheduler: back it up, patch it,
and restart it yourself like any other piece of infrastructure.
EOF
