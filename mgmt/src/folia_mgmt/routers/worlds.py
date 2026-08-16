"""World CRUD + snapshot/restore/migrate/access. PLAN.md §2, §10, §11B, §13."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.access_apply import UuidResolver, apply_ops, apply_whitelist
from folia_mgmt.auth import require_operator, require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.datapack_catalog import load_catalog as load_datapack_catalog
from folia_mgmt.db import get_session
from folia_mgmt.deps import get_health_check, get_lxd_client, get_uuid_resolver, settings_dependency
from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, HostStatus, World, WorldPhase, WorldType, utcnow
from folia_mgmt.plugin_catalog import load_catalog
from folia_mgmt.scheduler import HealthCheck, allocated_capacity, reconcile, _node_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/worlds", tags=["worlds"])


class CreateWorldRequest(BaseModel):
    name: str
    type: WorldType
    engine: str = "folia"
    version: str = "1.21.4"
    plugins: list[str] = []
    datapacks: list[str] = []
    properties: dict[str, str] = {}
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
    datapacks: list[str]
    properties: dict[str, str]
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
        datapacks=world.datapacks,
        properties=world.properties,
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


def _validate_plugins(plugins: list[str], settings: Settings) -> None:
    """Every plugin a world declares must be a real catalog entry — no
    more free-typed names that only fail (silently, at the JVM, at boot)
    if they don't match anything real. PLAN.md §14."""
    if not plugins:
        return
    if not settings.public_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "cannot declare plugins: FOLIA_MGMT_PUBLIC_URL is not configured, "
            "so worlds have no reachable URL to fetch their plugin manifest from",
        )
    catalog_ids = {entry.id for entry in load_catalog(settings)}
    unknown = [p for p in plugins if p not in catalog_ids]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown plugin(s), not in the catalog: {', '.join(unknown)}"
        )


def _validate_datapacks(datapacks: list[str], settings: Settings) -> None:
    """Same reasoning as _validate_plugins — every declared datapack must
    be a real catalog entry, and a world can't declare one without a
    reachable public_url to serve its datapacks-manifest from."""
    if not datapacks:
        return
    if not settings.public_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "cannot declare datapacks: FOLIA_MGMT_PUBLIC_URL is not configured, "
            "so worlds have no reachable URL to fetch their datapacks manifest from",
        )
    catalog_ids = {entry.id for entry in load_datapack_catalog(settings)}
    unknown = [d for d in datapacks if d not in catalog_ids]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown datapack(s), not in the catalog: {', '.join(unknown)}"
        )


# server.properties keys folia_node.staging either writes unconditionally
# itself (online-mode) or assumes a fixed value for elsewhere in the
# codebase (level-name — see staging.py's LEVEL_NAME comment on why data
# pack staging can't handle a custom one), plus the rcon.* keys reserved
# for when mgmt actually wires up RCON. An operator-set value for any of
# these would either be silently overwritten (online-mode) or break
# something non-obvious (level-name), so reject them outright rather than
# accept-and-ignore.
_PROTECTED_PROPERTIES = {"online-mode", "level-name", "server-port", "enable-rcon", "rcon.password", "rcon.port"}


def _validate_properties(properties: dict[str, str]) -> None:
    protected = _PROTECTED_PROPERTIES & properties.keys()
    if protected:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"these server.properties keys are managed by folia-nexa-mgmt/node, not operator-settable: "
            f"{', '.join(sorted(protected))}",
        )
    for key, value in properties.items():
        if not key or "=" in key or "\n" in key:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid server.properties key: {key!r}")
        if "\n" in value:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid server.properties value for {key!r}")


def _with_default_plugins(plugins: list[str], settings: Settings) -> list[str]:
    """Every world gets every catalog entry flagged
    default_for_all_worlds=true (e.g. FoliaNexaStats, HuskHomes) whether or
    not it was explicitly requested — order: explicit selections first
    (in the order given), then any missing defaults appended, so an
    operator's own ordering isn't disturbed. Only called once public_url
    is already known to be set (both call sites validate plugins first)."""
    defaults = [entry.id for entry in load_catalog(settings) if entry.default_for_all_worlds]
    merged = list(plugins)
    for plugin_id in defaults:
        if plugin_id not in merged:
            merged.append(plugin_id)
    return merged


@router.post("", response_model=WorldResponse, dependencies=[Depends(require_operator)])
def create_world(
    body: CreateWorldRequest,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    health_check: HealthCheck = Depends(get_health_check),
    settings: Settings = Depends(settings_dependency),
) -> WorldResponse:
    if session.exec(select(World).where(World.name == body.name)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"world '{body.name}' already exists")
    # Merge in cluster-wide default plugins *before* validating, not after
    # — so a cluster with no FOLIA_MGMT_PUBLIC_URL configured fails loudly
    # here rather than silently creating a world that claims a default
    # plugin it can never actually fetch.
    plugins = _with_default_plugins(body.plugins, settings)
    _validate_plugins(plugins, settings)
    _validate_datapacks(body.datapacks, settings)
    _validate_properties(body.properties)

    world = World(
        name=body.name,
        type=body.type,
        engine=body.engine,
        version=body.version,
        plugins=plugins,
        datapacks=body.datapacks,
        properties=body.properties,
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


class UpdateWorldRequest(BaseModel):
    # None means "leave unchanged" for each field — an explicit [] / {}
    # means "clear it", same convention as AccessUpdateRequest below.
    plugins: list[str] | None = None
    datapacks: list[str] | None = None
    properties: dict[str, str] | None = None


@router.patch("/{name}", response_model=WorldResponse, dependencies=[Depends(require_operator)])
def update_world(
    name: str,
    body: UpdateWorldRequest,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    settings: Settings = Depends(settings_dependency),
) -> WorldResponse:
    """Edits an already-declared world's plugins/datapacks/server.properties.
    Takes effect on the world's *next restart* — folia-nexa-node only
    re-syncs this config at its own startup (folia_node.staging), not
    live, and this endpoint doesn't restart the container itself (a
    world mid-restart loses whoever's currently playing on it, so that's
    an explicit separate action — see POST /{name}/restart).

    Cluster-wide default plugins (default_for_all_worlds in the catalog)
    are re-merged in on every update, same as at creation — an operator
    can't accidentally drop FoliaNexaStats/HuskHomes by editing plugins
    without them in the list.
    """
    world = _get_world_or_404(session, name)

    if body.plugins is not None:
        plugins = _with_default_plugins(body.plugins, settings)
        _validate_plugins(plugins, settings)
        world.plugins = plugins
    if body.datapacks is not None:
        _validate_datapacks(body.datapacks, settings)
        world.datapacks = body.datapacks
    if body.properties is not None:
        _validate_properties(body.properties)
        world.properties = body.properties
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    session.refresh(world)

    if world.host_name and world.container_name:
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is not None:
            try:
                lxd_client.update_config(host, world.container_name, _node_config(world, settings))
            except LXDError:
                logger.exception(
                    "world '%s' updated in mgmt's DB but failed to push config to its container — "
                    "it'll stay out of sync until this is retried",
                    name,
                )

    return _to_response(world)


@router.post("/{name}/restart", dependencies=[Depends(require_operator)])
def restart_world(
    name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> dict[str, str]:
    """Restarts a world's container so it picks up config pushed by
    PATCH /{name} (new/changed plugins, datapacks, or server.properties)
    — folia-nexa-node only re-syncs that config at its own startup."""
    world, host = _host_and_world(session, name)
    try:
        lxd_client.restart_container(host, world.container_name)
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"restarted": world.container_name}


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


@router.post("/{name}/migrate", response_model=WorldResponse, dependencies=[Depends(require_operator)])
def migrate_world(
    name: str,
    target_host: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> WorldResponse:
    """Stop -> export -> import -> start on the target, then cut mgmt's
    state over and delete the source container. PLAN.md §13. A brief
    outage is expected — this isn't a live migration (see
    LXDClient.migrate_container's docstring for why)."""
    world, source_host = _host_and_world(session, name)

    target = session.exec(select(Host).where(Host.name == target_host)).first()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such host '{target_host}'")
    if target.name == source_host.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"world '{name}' is already on '{target_host}'")
    if target.status != HostStatus.online:
        raise HTTPException(status.HTTP_409_CONFLICT, f"target host '{target_host}' is not online")

    used_cpu, used_mem = allocated_capacity(session, target.name)
    if (
        target.capacity_cpu_cores - used_cpu < world.cpu_cores
        or target.capacity_memory_gb - used_mem < world.memory_gb
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"target host '{target_host}' doesn't have capacity for world '{name}'"
        )

    try:
        lxd_client.migrate_container(source_host, world.container_name, target)
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"migration failed, world stays on '{source_host.name}': {exc}") from exc

    # Cutover: same container name, new host. phase=provisioning so the
    # next reconcile pass re-polls for the (new) address before flipping
    # back to running, same as any fresh placement.
    world.host_name = target.name
    world.sticky_host = target.name
    world.phase = WorldPhase.provisioning
    world.address = None
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    session.refresh(world)

    try:
        lxd_client.delete_container(source_host, world.container_name)
    except LXDError:
        logger.exception(
            "migrated world '%s' to '%s' but failed to delete the old container on '%s' — clean up manually",
            name,
            target.name,
            source_host.name,
        )

    return _to_response(world)


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


@router.get("/{name}/plugins-manifest")
def get_plugins_manifest(
    name: str, session: Session = Depends(get_session), settings: Settings = Depends(settings_dependency)
) -> list[dict]:
    """What folia-nexa-node actually fetches to stage a world's plugins
    (PLAN.md §9, §14) — generated live from world.plugins + the catalog,
    replacing the old hand-authored-manifest-file approach entirely.

    Deliberately unauthenticated: node has no mgmt credential of any kind
    by design (PLAN.md §9 — its own instance config is its registration,
    no outbound auth to mgmt), and this only ever exposes plugin names
    and their already-public download URLs — nothing sensitive. Matches
    node's existing unauthenticated GET of the jar/manifest from an
    external artifacts host; this just replaces that host for manifests.
    """
    world = _get_world_or_404(session, name)
    catalog = {entry.id: entry for entry in load_catalog(settings)}

    manifest = []
    for plugin_id in world.plugins:
        entry = catalog.get(plugin_id)
        if entry is None or entry.download_url is None:
            logger.warning(
                "world '%s' declares plugin '%s' with no resolvable download_url in the catalog, skipping",
                name,
                plugin_id,
            )
            continue
        manifest.append({"name": entry.id, "url": entry.download_url})
    return manifest


@router.get("/{name}/server-properties-manifest")
def get_server_properties_manifest(name: str, session: Session = Depends(get_session)) -> dict[str, str]:
    """What folia-nexa-node fetches to reconcile server.properties on every
    (re)start — same reasoning as get_plugins_manifest above (deliberately
    unauthenticated, node has no mgmt credential). Only ever contains
    operator-set overrides (_validate_properties already rejects the
    protected keys node manages itself); node is responsible for merging
    this over its own required baseline, not the other way around, so a
    world with no overrides at all just gets an empty object here.
    """
    world = _get_world_or_404(session, name)
    return world.properties


@router.get("/{name}/datapacks-manifest")
def get_datapacks_manifest(
    name: str, session: Session = Depends(get_session), settings: Settings = Depends(settings_dependency)
) -> list[dict]:
    """What folia-nexa-node fetches to stage a world's data packs — same
    shape and same reasoning as get_plugins_manifest above (generated live
    from world.datapacks + the data pack catalog, deliberately
    unauthenticated for the same reasons). Node places each entry under
    the world save's `datapacks/` folder instead of `plugins/` — see
    folia_node.staging.
    """
    world = _get_world_or_404(session, name)
    catalog = {entry.id: entry for entry in load_datapack_catalog(settings)}

    manifest = []
    for datapack_id in world.datapacks:
        entry = catalog.get(datapack_id)
        if entry is None or entry.download_url is None:
            logger.warning(
                "world '%s' declares datapack '%s' with no resolvable download_url in the catalog, skipping",
                name,
                datapack_id,
            )
            continue
        manifest.append({"name": entry.id, "url": entry.download_url})
    return manifest
