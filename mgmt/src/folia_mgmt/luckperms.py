"""Keeps every LuckPerms-enabled world's config.yml pointed at the same
shared MySQL/MariaDB backend. PLAN.md §11B.

This does **not** stand up the MySQL/MariaDB instance itself —
folia-smp-node only knows how to run a Folia/Paper JVM (see
node/src/folia_node/agent.py), not arbitrary services, so scheduling a
database server as a `type: infra` world isn't something the current
node agent can do. The operator provisions that instance separately
(see configs/luckperms/provision-mysql.sh for a starting point) and
points mgmt at it via the `luckperms_mysql_*` settings; what mgmt
automates is every world's plugin config staying in sync with it, so
groups/permissions/tracks stay consistent across worlds without hand-
editing each one.

One real limitation, not hidden: LuckPerms reads its storage backend
from config.yml at plugin load time, not on a live reload, so a world
whose LuckPerms config changes (first sync after a fresh install, or the
shared DB's credentials changing) needs a restart to actually pick it
up. This module pushes the file; it doesn't restart anything — the next
world restart (crash-recovery, an operator-initiated one, or a future
Paper reload command) is what makes a changed config take effect.
"""

from __future__ import annotations

import logging

from folia_mgmt.access_apply import NODE_WORLD_DIR
from folia_mgmt.config import Settings
from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, World

logger = logging.getLogger(__name__)

LUCKPERMS_CONFIG_PATH = f"{NODE_WORLD_DIR}/plugins/LuckPerms/config.yml"


def render_luckperms_config(world_name: str, settings: Settings) -> bytes:
    """LuckPerms' own config.yml shape for MySQL storage. Written against
    LuckPerms' documented config format — verify against the LuckPerms
    version you actually deploy, since plugin config keys can drift
    between major versions and this hasn't been validated against a
    running instance.

    `server` is set to the world's own name so LuckPerms' per-server
    context tracking (and its web editor) can tell worlds apart; every
    world sharing this `table_prefix` and connection is what makes
    permissions actually sync across them.
    """
    return (
        "storage-method: mysql\n"
        "\n"
        "data:\n"
        f"  address: {settings.luckperms_mysql_host}:{settings.luckperms_mysql_port}\n"
        f"  database: {settings.luckperms_mysql_database}\n"
        f"  username: {settings.luckperms_mysql_user}\n"
        f"  password: {settings.luckperms_mysql_password}\n"
        "  pool-settings:\n"
        "    maximum-pool-size: 10\n"
        "    minimum-idle: 10\n"
        "    maximum-lifetime: 1800000\n"
        "    connection-timeout: 5000\n"
        "\n"
        f"table_prefix: '{settings.luckperms_table_prefix}'\n"
        "split-storage:\n"
        "  enabled: false\n"
        "\n"
        f"server: '{world_name}'\n"
    ).encode()


def apply_luckperms_config(lxd_client: LXDClient, host: Host, world: World, settings: Settings) -> None:
    if not settings.luckperms_configured or not world.container_name:
        return
    if "LuckPerms" not in world.plugins:
        return
    content = render_luckperms_config(world.name, settings)
    try:
        lxd_client.push_file(host, world.container_name, LUCKPERMS_CONFIG_PATH, content)
    except LXDError:
        # Expected to fail (and get retried next reconcile tick) until
        # LuckPerms itself has run once and created its plugin data
        # folder — see module docstring.
        logger.debug(
            "could not push LuckPerms config for world '%s' yet (plugin folder may not exist), will retry",
            world.name,
        )
