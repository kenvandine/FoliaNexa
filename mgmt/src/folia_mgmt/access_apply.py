"""Applies World.ops to a running container's ops.json. PLAN.md §11B.

whitelist_enabled isn't applied to a live container here — there's no
per-world list of *who* is whitelisted separate from network-wide Discord
approval (§11C), and pushing an empty whitelist.json while flipping
whitelist_enabled=true would lock everyone out rather than anyone in.
Making that coherent needs a product decision this codebase hasn't made
yet: give worlds their own entries list, or mirror the same
approved-uuids set the proxy's access gate (§8C) already uses. Tracked as
a follow-up rather than shipped half-working.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, World

logger = logging.getLogger(__name__)

# Matches folia-smp-node's WORLD_DIR ($SNAP_COMMON/world, node/snapcraft.yaml).
NODE_WORLD_DIR = "/var/snap/folia-smp-node/common/world"

UuidResolver = Callable[[str], str | None]


def build_ops_json(op_names: list[str], resolve_uuid: UuidResolver) -> bytes:
    """Paper's ops.json shape: [{"uuid", "name", "level", "bypassesPlayerLimit"}, ...]."""
    entries = []
    for name in op_names:
        uuid = resolve_uuid(name)
        if uuid is None:
            logger.warning("could not resolve a Minecraft UUID for op '%s', skipping", name)
            continue
        entries.append({"uuid": uuid, "name": name, "level": 4, "bypassesPlayerLimit": False})
    return json.dumps(entries).encode()


def apply_ops(lxd_client: LXDClient, host: Host, world: World, resolve_uuid: UuidResolver) -> None:
    if not world.container_name:
        return
    content = build_ops_json(world.ops, resolve_uuid)
    try:
        lxd_client.push_file(host, world.container_name, f"{NODE_WORLD_DIR}/ops.json", content)
    except LXDError:
        logger.exception(
            "failed to push ops.json for world '%s' — DB updated, live container not yet in sync", world.name
        )
