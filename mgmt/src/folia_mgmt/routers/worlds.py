"""World CRUD + snapshot/restore/migrate/access. PLAN.md §2, §10, §11B, §13."""

from __future__ import annotations

import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.access_apply import UuidResolver, apply_ops, apply_whitelist
from folia_mgmt.auth import require_operator, require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.datapack_catalog import load_catalog as load_datapack_catalog
from folia_mgmt.db import get_session
from folia_mgmt.deps import get_lxd_client, get_uuid_resolver, settings_dependency
from folia_mgmt.lxd_client import LXDClient, LXDError
from folia_mgmt.models import Host, HostStatus, MinecraftVersionConfig, World, WorldPhase, WorldType, utcnow
from folia_mgmt.routers.cluster import migrate_world_to_current_version
from folia_mgmt.plugin_catalog import load_catalog
from folia_mgmt.rcon import RconError, execute_rcon_command
from folia_mgmt.scheduler import allocated_capacity, finalize_provisioning, place_world, teardown_world, _node_config

# Minecraft's conventional RCON port — not configurable per-world today
# (mirrors the fixed 25565 game port every world already uses), just a
# named constant instead of a magic number at the one call site that
# needs it.
RCON_PORT = 25575

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/worlds", tags=["worlds"])


class CreateWorldRequest(BaseModel):
    name: str
    type: WorldType
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


def _place_best_effort(session: Session, lxd_client: LXDClient, world: World, settings: Settings) -> None:
    """Immediate placement attempt for just this one world, using the
    request's own DI-provided session/LXD client, so a newly created world
    doesn't sit in 'pending' for up to reconcile's periodic interval. This
    used to call the full reconcile() here instead — which also
    synchronously re-health-checked every *other* running world in the
    cluster (plus whitelist/LuckPerms/stats sync for all of them) inside
    this one request, making world creation feel unresponsive for several
    seconds on anything but a tiny cluster, for work that has nothing to
    do with the world being created. A slow/unreachable host still bounds
    the delay to that one host's LXD request timeout rather than hanging
    indefinitely; the periodic loop retries regardless if this fails."""
    try:
        place_world(session, lxd_client, world, settings)
        # place_world only gets as far as 'provisioning' (LXD hasn't
        # necessarily handed out an address yet) — also try the very next
        # step for this same world, same as a full reconcile() would have,
        # so a world that can go all the way to 'running' in one shot
        # still does rather than sitting in 'provisioning' until the next
        # periodic tick.
        if world.phase == WorldPhase.provisioning:
            finalize_provisioning(session, lxd_client, world)
    except Exception:
        logger.exception("immediate placement of '%s' failed; periodic loop will retry", world.name)


def _teardown_best_effort(session: Session, lxd_client: LXDClient, world: World) -> None:
    """Same idea as _place_best_effort, for deletion — tears down just this
    world's container immediately rather than waiting on the periodic
    loop's next draining sweep."""
    try:
        teardown_world(session, lxd_client, world)
    except Exception:
        logger.exception("immediate teardown of '%s' failed; periodic loop will retry", world.name)


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


def _ensure_rcon_password(world: World) -> None:
    """Lazily generates World.rcon_password the first time it's needed —
    called from both create_world and update_world so a world declared
    before RCON existed still gets one on its next edit, not just brand
    new worlds. Idempotent: a no-op once set."""
    if not world.rcon_password:
        world.rcon_password = secrets.token_urlsafe(32)


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

    mc = session.get(MinecraftVersionConfig, 1) or MinecraftVersionConfig()
    world = World(
        name=body.name,
        type=body.type,
        engine=mc.engine,
        version=mc.version,
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
    _ensure_rcon_password(world)
    session.add(world)
    session.commit()
    session.refresh(world)

    _place_best_effort(session, lxd_client, world, settings)
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
    _ensure_rcon_password(world)  # backfills worlds declared before RCON existed
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    session.refresh(world)

    if world.host_name and world.container_name:
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is not None:
            try:
                lxd_client.update_config(host, world.container_name, _node_config(session, world, settings))
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


@router.post("/{name}/stop", dependencies=[Depends(require_operator)])
def stop_world(
    name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> dict[str, str]:
    """Stops a world's container without deleting it — the container and
    its save data stay put, just powered off, until /start brings it back.
    Distinct from DELETE (tears the container down for good) and from a
    crash (mgmt tries to auto-recover those): a deliberately stopped world
    sits in phase 'stopped', which check_running_worlds and
    recover_crashed_worlds don't touch (they only act on 'running'/
    'crashed'), so the reconcile loop leaves it alone until /start."""
    world, host = _host_and_world(session, name)
    if world.phase not in (WorldPhase.running, WorldPhase.crashed):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"world '{name}' is not running (phase: {world.phase.value})"
        )
    try:
        lxd_client.stop_container(host, world.container_name)
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    world.phase = WorldPhase.stopped
    world.address = None
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    return {"stopped": world.container_name}


@router.post("/{name}/start", dependencies=[Depends(require_operator)])
def start_world(
    name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> dict[str, str]:
    """Starts a previously-stopped world's container back up. Also makes
    one immediate finalize_provisioning attempt for this world (same
    best-effort pattern as world creation — see _place_best_effort) so it
    can reach 'running' in this same request rather than sitting in
    'provisioning' until the next periodic reconcile tick."""
    world, host = _host_and_world(session, name)
    if world.phase != WorldPhase.stopped:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"world '{name}' is not stopped (phase: {world.phase.value})"
        )
    try:
        lxd_client.start_container(host, world.container_name)
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    world.phase = WorldPhase.provisioning
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    try:
        finalize_provisioning(session, lxd_client, world)
    except Exception:
        logger.exception("immediate finalize after starting '%s' failed; periodic loop will retry", name)
    return {"started": world.container_name}


class MigrateVersionResponse(BaseModel):
    migrated: bool
    detail: str | None = None


@router.post(
    "/{name}/migrate-minecraft-version",
    response_model=MigrateVersionResponse,
    dependencies=[Depends(require_operator)],
)
def migrate_world_minecraft_version(
    name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    settings: Settings = Depends(settings_dependency),
) -> MigrateVersionResponse:
    """Migrates just this one world to the cluster's current Minecraft
    version — the per-world counterpart to POST /cluster/minecraft-
    version/migrate, for rolling a version bump out world-by-world
    (e.g. checking a lobby migrates cleanly before touching everything
    else) rather than all at once."""
    world = _get_world_or_404(session, name)
    config = session.get(MinecraftVersionConfig, 1) or MinecraftVersionConfig(id=1)
    result = migrate_world_to_current_version(session, lxd_client, settings, config, world)
    return MigrateVersionResponse(migrated=result.migrated, detail=result.detail)


class RconCommandRequest(BaseModel):
    command: str


@router.post("/{name}/rcon", dependencies=[Depends(require_operator)])
def run_rcon_command(
    name: str,
    body: RconCommandRequest,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """Runs one command against a running world's live server via RCON —
    the instant-apply counterpart to PATCH /{name}'s restart-to-apply
    config edits. Only useful for things Minecraft itself lets you change
    without a restart (/gamerule, /difficulty, /whitelist, /op, /kick,
    /say, ...) — server.properties keys like allow-flight have no live
    command equivalent at all and still need PATCH + restart regardless.

    world.address is the same container IP the Minecraft port itself is
    reachable on (mgmt already has direct network reachability to every
    placed world — see default_health_check) — RCON is just a different
    port on that same address, not a separate LXD round trip.
    """
    world = _get_world_or_404(session, name)
    if not world.address:
        raise HTTPException(status.HTTP_409_CONFLICT, f"world '{name}' is not placed on a host yet")
    if not world.rcon_password:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"world '{name}' has no RCON password yet — PATCH it (even a no-op edit) to generate one, "
            "then restart the world so it picks up RCON config",
        )
    host = world.address.split(":", 1)[0]
    try:
        response = execute_rcon_command(host, RCON_PORT, world.rcon_password, body.command)
    except RconError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return {"response": response}


def _stream_world_log(name: str, log_type: str, session: Session, settings: Settings) -> StreamingResponse:
    """Shared body for the two /logs endpoints below — proxies
    folia-nexa-node's own /logs/{log_type}/stream (health.py's
    LogBroadcaster, PLAN.md §9) straight through to the caller. Plain
    chunked text, not re-framed — matches node's wire format exactly
    since the dashboard reads it as a raw byte stream, not SSE.

    world.address is re-resolved fresh on every call (not cached) so a
    reconnect after a migration/restart picks up the world's current IP
    rather than proxying to a stale one.
    """
    world = _get_world_or_404(session, name)
    if not world.address:
        raise HTTPException(status.HTTP_409_CONFLICT, f"world '{name}' is not placed on a host yet")
    host = world.address.split(":", 1)[0]
    url = f"http://{host}:{settings.node_health_port}/logs/{log_type}/stream"

    # A non-200 here (most commonly the node agent 404ing because it
    # predates this /logs endpoint — see health.py's do_GET) must not be
    # forwarded as streamed body content under our own 200: the node
    # agent's JSON error body would otherwise render in the log panel as
    # if it were real log output, with no way to tell it apart. Check the
    # status from the same stream we're about to consume rather than a
    # separate probe request — on success this endpoint never closes
    # (node's keepalive loop), so a second blocking GET would just hang.
    client = httpx.Client(timeout=None)
    try:
        stream_ctx = client.stream("GET", url)
        resp = stream_ctx.__enter__()
    except httpx.HTTPError as exc:
        client.close()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not reach folia-nexa-node on '{host}': {exc}"
        ) from exc

    if resp.status_code != 200:
        body_preview = resp.read()[:200]
        stream_ctx.__exit__(None, None, None)
        client.close()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"folia-nexa-node on '{host}' returned {resp.status_code} for {log_type} logs "
            f"(node agent may predate this endpoint — check its snap revision): {body_preview!r}",
        )

    def generate():
        try:
            yield from resp.iter_bytes()
        finally:
            stream_ctx.__exit__(None, None, None)
            client.close()

    return StreamingResponse(generate(), media_type="text/plain")


@router.get("/{name}/logs/console", dependencies=[Depends(require_viewer)])
def stream_console_log(
    name: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
) -> StreamingResponse:
    """Live Folia/Paper server console (JVM stdout) — plugin activity,
    player joins, crashes. See _stream_world_log."""
    return _stream_world_log(name, "console", session, settings)


@router.get("/{name}/logs/agent", dependencies=[Depends(require_viewer)])
def stream_agent_log(
    name: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
) -> StreamingResponse:
    """Live folia-nexa-node operational log — staging, jar downloads,
    devlxd/config sync. Distinct from the server console: the failures
    debugged in this cluster historically (a 404'd jar download, a TLS
    scheme mismatch on the manifest fetch) only ever showed up here,
    before the JVM ever started. See _stream_world_log."""
    return _stream_world_log(name, "agent", session, settings)


@router.delete("/{name}", response_model=WorldResponse, dependencies=[Depends(require_operator)])
def delete_world(
    name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
) -> WorldResponse:
    world = _get_world_or_404(session, name)
    world_id = world.id
    world.phase = WorldPhase.draining
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    session.refresh(world)
    draining_snapshot = _to_response(world)

    _teardown_best_effort(session, lxd_client, world)

    # teardown_world (scheduler.py) hard-deletes the row once its
    # container is actually gone — freeing the name for reuse. Refreshing
    # `world` here would raise on an object no longer in the DB, so check
    # first: gone means report the pre-teardown snapshot with phase
    # overridden to "deleted"; still there means teardown hasn't
    # completed yet (e.g. the host was unreachable), so reflect its real
    # current phase instead.
    still_present = session.get(World, world_id)
    if still_present is None:
        return draining_snapshot.model_copy(update={"phase": WorldPhase.deleted.value})
    session.refresh(still_present)
    return _to_response(still_present)


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
