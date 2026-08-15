"""Applies World.ops and World.whitelist_enabled to a running container's
ops.json/whitelist.json. PLAN.md §11B.

whitelist_enabled doesn't get its own per-world membership list — that
would mean maintaining two separate approval lists (this one, and §11C's
network-wide Discord-approved set) that could drift apart. Instead, a
world with whitelist_enabled=true mirrors exactly the same approved set
the proxy's access gate (§8C) already polls: `AccessRequest` rows with
status=approved. Turning the toggle on is "also enforce network approval
at this world's Paper level, as defense in depth beyond the proxy gate,"
not "give this world its own separate guest list."

One real gap this doesn't close: pushing whitelist.json changes its
*membership*, but actually toggling Paper's enforcement of it lives in
server.properties (`white-list=true/false`), which nothing here manages
yet — that would need either templating server.properties at container
launch or an RCON command. Tracked as a follow-up.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from sqlmodel import Session, select

from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import AccessRequest, AccessRequestStatus, Host, World

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


def build_whitelist_json(session: Session) -> bytes:
    """Paper's whitelist.json shape: [{"uuid", "name"}, ...] — mirrors the
    same approved set §8C's proxy access gate uses, see module docstring."""
    approved = session.exec(
        select(AccessRequest).where(
            AccessRequest.status == AccessRequestStatus.approved,
            AccessRequest.minecraft_uuid.is_not(None),
            AccessRequest.minecraft_username.is_not(None),
        )
    ).all()
    entries = [{"uuid": r.minecraft_uuid, "name": r.minecraft_username} for r in approved]
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


def apply_whitelist(session: Session, lxd_client: LXDClient, host: Host, world: World) -> None:
    if not world.container_name or not world.whitelist_enabled:
        return
    content = build_whitelist_json(session)
    try:
        lxd_client.push_file(host, world.container_name, f"{NODE_WORLD_DIR}/whitelist.json", content)
    except LXDError:
        logger.exception(
            "failed to push whitelist.json for world '%s' — DB updated, live container not yet in sync",
            world.name,
        )
