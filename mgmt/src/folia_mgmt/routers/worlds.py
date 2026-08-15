"""World CRUD + snapshot/restore/migrate/access. PLAN.md §2, §10, §11B, §13."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.access_apply import UuidResolver, apply_ops, apply_whitelist
from folia_mgmt.auth import require_operator, require_viewer
from folia_mgmt.db import get_session
from folia_mgmt.deps import get_health_check, get_lxd_client, get_uuid_resolver
from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, World, WorldPhase, WorldType, utcnow
from folia_mgmt.scheduler import HealthCheck, reconcile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/worlds", tags=["worlds"])


class CreateWorldRequest(BaseModel):
    name: str
    type: WorldType
    engine: str = "folia"
    version: str = "1.21.4"
    plugins: list[str] = []
    cpu_cores: int
    memory_gb: int
    placement_labels: dict[str, str] = {}
    snapshot_schedule: str | None = None
    snapshot_expiry: str | None = None


class WorldResponse(BaseModel):
    name: str
    type: str
    engine: str
    version: str
    plugins: list[str]
    cpu_cores: int
    memory_gb: int
    placement_labels: dict[str, str]
    phase: str
    host_name: str | None
    container_name: str | None
    address: str | None
    whitelist_enabled: bool
    ops: list[str]


def _to_response(world: World) -> WorldResponse:
    return WorldResponse(
        name=world.name,
        type=world.type.value,
        engine=world.engine,
        version=world.version,
        plugins=world.plugins,
        cpu_cores=world.cpu_cores,
        memory_gb=world.memory_gb,
        placement_labels=world.placement_labels,
        phase=world.phase.value,
        host_name=world.host_name,
        container_name=world.container_name,
        address=world.address,
        whitelist_enabled=world.whitelist_enabled,
        ops=world.ops,
    )


def _reconcile_best_effort(session: Session, lxd_client: LXDClient, health_check: HealthCheck) -> None:
    """Immediate placement/teardown attempt using the request's own DI-
    provided session, LXD client, and health checker, so behavior (and test
    overrides) match exactly what the periodic loop in main.py does. A
    slow/unreachable host delays the response by however long that one
    host's HTTP call takes (bounded by LXDClient's request timeout) rather
    than hanging indefinitely; the periodic loop retries regardless if this
    fails."""
    try:
        reconcile(session, lxd_client, health_check=health_check)
    except Exception:
        logger.exception("immediate reconcile after world create/delete failed; periodic loop will retry")


def _get_world_or_404(session: Session, name: str) -> World:
    world = session.exec(select(World).where(World.name == name)).first()
    if world is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such world '{name}'")
    return world


@router.post("", response_model=WorldResponse, dependencies=[Depends(require_operator)])
def create_world(
    body: CreateWorldRequest,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    health_check: HealthCheck = Depends(get_health_check),
) -> WorldResponse:
    if session.exec(select(World).where(World.name == body.name)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"world '{body.name}' already exists")

    world = World(
        name=body.name,
        type=body.type,
        engine=body.engine,
        version=body.version,
        plugins=body.plugins,
        cpu_cores=body.cpu_cores,
        memory_gb=body.memory_gb,
        placement_labels=body.placement_labels,
        snapshot_schedule=body.snapshot_schedule,
        snapshot_expiry=body.snapshot_expiry,
        phase=WorldPhase.pending,
    )
    session.add(world)
    session.commit()
    session.refresh(world)

    _reconcile_best_effort(session, lxd_client, health_check)
    session.refresh(world)
    return _to_response(world)


@router.get("", response_model=list[WorldResponse], dependencies=[Depends(require_viewer)])
def list_worlds(session: Session = Depends(get_session)) -> list[WorldResponse]:
    return [_to_response(w) for w in session.exec(select(World)).all()]


@router.delete("/{name}", response_model=WorldResponse, dependencies=[Depends(require_operator)])
def delete_world(
    name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    health_check: HealthCheck = Depends(get_health_check),
) -> WorldResponse:
    world = _get_world_or_404(session, name)
    world.phase = WorldPhase.draining
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    session.refresh(world)

    _reconcile_best_effort(session, lxd_client, health_check)
    session.refresh(world)
    return _to_response(world)


def _host_and_world(session: Session, name: str) -> tuple[World, Host]:
    world = _get_world_or_404(session, name)
    if not world.host_name or not world.container_name:
        raise HTTPException(status.HTTP_409_CONFLICT, f"world '{name}' is not placed on a host yet")
    host = session.exec(select(Host).where(Host.name == world.host_name)).first()
    if host is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"world '{name}' points at unknown host")
    return world, host


@router.post("/{name}/snapshot", dependencies=[Depends(require_operator)])
def snapshot_world(
    name: str,
    snapshot_name: str | None = None,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> dict[str, str]:
    world, host = _host_and_world(session, name)
    label = snapshot_name or f"manual-{int(utcnow().timestamp())}"
    try:
        lxd_client.snapshot_container(host, world.container_name, label)
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"snapshot": label}


@router.post("/{name}/restore/{snapshot_name}", dependencies=[Depends(require_operator)])
def restore_world(
    name: str,
    snapshot_name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> dict[str, str]:
    world, host = _host_and_world(session, name)
    try:
        lxd_client.restore_snapshot(host, world.container_name, snapshot_name)
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"restored": snapshot_name}


@router.post("/{name}/migrate", dependencies=[Depends(require_operator)])
def migrate_world(name: str, target_host: str, session: Session = Depends(get_session)) -> None:
    # See PLAN.md §13 — snapshot -> lxc copy to target host -> cut over.
    # LXDClient.copy_snapshot is not implemented yet (untested against a
    # live multi-host LXD setup); fail loudly instead of pretending to work.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "cross-host migration not implemented yet — see PLAN.md §13")


class AccessUpdateRequest(BaseModel):
    whitelist_enabled: bool | None = None
    ops: list[str] | None = None


@router.get("/{name}/access", dependencies=[Depends(require_viewer)])
def get_world_access(name: str, session: Session = Depends(get_session)) -> dict:
    world = _get_world_or_404(session, name)
    return {"whitelist_enabled": world.whitelist_enabled, "ops": world.ops}


@router.put("/{name}/access", dependencies=[Depends(require_operator)])
def put_world_access(
    name: str,
    body: AccessUpdateRequest,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    uuid_resolver: UuidResolver = Depends(get_uuid_resolver),
) -> dict:
    # whitelist_enabled=true mirrors the network-wide Discord-approved set
    # (§11C) rather than a separate per-world guest list — see
    # access_apply.py's module docstring for the full reasoning.
    world = _get_world_or_404(session, name)
    if body.whitelist_enabled is not None:
        world.whitelist_enabled = body.whitelist_enabled
    if body.ops is not None:
        world.ops = body.ops
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    session.refresh(world)

    if (body.ops is not None or body.whitelist_enabled is not None) and world.host_name and world.container_name:
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is not None:
            if body.ops is not None:
                apply_ops(lxd_client, host, world, uuid_resolver)
            if body.whitelist_enabled is not None:
                apply_whitelist(session, lxd_client, host, world)

    return {"whitelist_enabled": world.whitelist_enabled, "ops": world.ops}
