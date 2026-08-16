"""Cluster-wide settings — currently just the single Minecraft engine/
version every world runs. PLAN.md §9.

Deliberately cluster-wide, not per-world: every world shares one
Velocity proxy and (PLAN.md §14B) one lobby that routes players between
them, so they all need to speak the same protocol version — a client on
a newer version simply cannot connect to an older backend at all,
regardless of which world it targets. World.engine/World.version still
exist as per-world columns, but only for display/history now
(routers/worlds.py's _to_response) — what a world actually runs is
resolved from MinecraftVersionConfig at every placement/config-push
(scheduler.py's _node_config), never from the world row itself.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import require_operator, require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.db import get_session
from folia_mgmt.deps import get_lxd_client, settings_dependency
from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, MinecraftVersionConfig, World, WorldPhase, utcnow
from folia_mgmt.scheduler import _node_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cluster", tags=["cluster"])


def _get_config(session: Session) -> MinecraftVersionConfig:
    return session.get(MinecraftVersionConfig, 1) or MinecraftVersionConfig(id=1)


class MinecraftVersionResponse(BaseModel):
    engine: str
    version: str


@router.get(
    "/minecraft-version", response_model=MinecraftVersionResponse, dependencies=[Depends(require_viewer)]
)
def get_minecraft_version(session: Session = Depends(get_session)) -> MinecraftVersionResponse:
    config = _get_config(session)
    return MinecraftVersionResponse(engine=config.engine, version=config.version)


class UpdateMinecraftVersionRequest(BaseModel):
    engine: str = "folia"
    version: str


@router.put(
    "/minecraft-version", response_model=MinecraftVersionResponse, dependencies=[Depends(require_operator)]
)
def update_minecraft_version(
    body: UpdateMinecraftVersionRequest, session: Session = Depends(get_session)
) -> MinecraftVersionResponse:
    """Updates the cluster-wide target only — does NOT touch any already-
    placed world by itself (rolling out a version bump means real jar
    downloads and a restart per world, not something to do as a side
    effect of saving a settings form). New worlds created after this
    call pick it up immediately (routers/worlds.py's create_world); for
    already-placed worlds, see POST /minecraft-version/migrate."""
    config = _get_config(session)
    config.engine = body.engine
    config.version = body.version
    config.updated_at = utcnow()
    session.add(config)
    session.commit()
    session.refresh(config)
    return MinecraftVersionResponse(engine=config.engine, version=config.version)


class MigrateResult(BaseModel):
    world: str
    migrated: bool
    detail: str | None = None


class MigrateResponse(BaseModel):
    engine: str
    version: str
    results: list[MigrateResult]


def migrate_world_to_current_version(
    session: Session, lxd_client: LXDClient, settings: Settings, config: MinecraftVersionConfig, world: World
) -> MigrateResult:
    """Pushes the cluster's current engine/version to one running world's
    container config and restarts it so folia-nexa-node re-syncs
    (staging.py checks the staged jar's own version marker on every
    restart now, not just first boot — see sync_world_config) — same
    restart-to-apply pattern as PATCH /worlds/{name}. Shared by the bulk
    "migrate every running world" action below and routers/worlds.py's
    per-world POST /worlds/{name}/migrate-minecraft-version."""
    if not world.host_name or not world.container_name:
        return MigrateResult(world=world.name, migrated=False, detail="not placed")
    host = session.exec(select(Host).where(Host.name == world.host_name)).first()
    if host is None:
        return MigrateResult(world=world.name, migrated=False, detail="host not found")

    world.engine = config.engine
    world.version = config.version
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    session.refresh(world)

    try:
        lxd_client.update_config(host, world.container_name, _node_config(session, world, settings))
        lxd_client.restart_container(host, world.container_name)
        return MigrateResult(world=world.name, migrated=True)
    except LXDError as exc:
        logger.exception("failed to migrate world '%s' to %s %s", world.name, config.engine, config.version)
        return MigrateResult(world=world.name, migrated=False, detail=str(exc))


@router.post(
    "/minecraft-version/migrate", response_model=MigrateResponse, dependencies=[Depends(require_operator)]
)
def migrate_worlds_to_current_version(
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    settings: Settings = Depends(settings_dependency),
) -> MigrateResponse:
    """Migrates every currently-running world. Worlds not currently
    running (still pending, provisioning, or torn down) are skipped, not
    errored — they pick up the current cluster version whenever they do
    reach placement, via the normal creation/placement path, no
    migration needed."""
    config = _get_config(session)
    worlds = session.exec(select(World).where(World.phase == WorldPhase.running)).all()
    results = [migrate_world_to_current_version(session, lxd_client, settings, config, world) for world in worlds]
    return MigrateResponse(engine=config.engine, version=config.version, results=results)
