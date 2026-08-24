"""World CRUD + snapshot/restore/migrate/access. PLAN.md §2, §10, §11B, §13."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.access_apply import NODE_WORLD_DIR, UuidResolver, apply_ops, apply_whitelist
from folia_mgmt.auth import require_admin, require_operator, require_viewer
from folia_mgmt.config import Settings
from folia_mgmt.datapack_catalog import load_catalog as load_datapack_catalog
from folia_mgmt.db import get_session
from folia_mgmt.deps import get_lxd_client, get_uuid_resolver, settings_dependency
from folia_mgmt.lxd_client import LONG_OPERATION_TIMEOUT, RESTART_WAIT_TIMEOUT, LXDClient, LXDError, RestoreInProgressError
from folia_mgmt.models import (
    Host,
    HostStatus,
    MinecraftVersionConfig,
    User,
    UserRole,
    World,
    WorldBackup,
    WorldPhase,
    WorldType,
    epoch_seconds,
    utcnow,
)
from folia_mgmt.routers.cluster import migrate_world_to_current_version
from folia_mgmt.plugin_catalog import load_catalog
from folia_mgmt.rcon import RconError, execute_rcon_command
from folia_mgmt.scheduler import allocated_capacity, finalize_provisioning, place_world, teardown_world, _node_config
from folia_mgmt import world_backups

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
    backups_enabled: bool
    last_backup_attempt_at: datetime | None
    last_backup_error: str | None
    last_restore_confirmed_at: datetime | None
    last_restore_error: str | None


def _to_response(world: World, *, redact_backup_error: bool) -> WorldResponse:
    """`redact_backup_error` drops `last_backup_error`/`last_restore_error`
    for viewer-role callers — both carry raw `LXDError`/agent-side
    exception text (see scheduler.py's run_scheduled_backups and
    _record_restore_outcome), which is internal backend detail nobody
    below operator has otherwise been able to see through this API.
    No default: every call site must state its caller's role explicitly,
    so a future endpoint that opens this response to viewer-role callers
    can't forget to redact simply by not passing the keyword."""
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
        backups_enabled=world.backups_enabled,
        last_backup_attempt_at=world.last_backup_attempt_at,
        last_backup_error=None if redact_backup_error else world.last_backup_error,
        last_restore_confirmed_at=world.last_restore_confirmed_at,
        last_restore_error=None if redact_backup_error else world.last_restore_error,
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
            finalize_provisioning(session, lxd_client, world, settings)
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
    return _to_response(world, redact_backup_error=False)


@router.get("", response_model=list[WorldResponse])
def list_worlds(
    session: Session = Depends(get_session),
    user: User = Depends(require_viewer),
) -> list[WorldResponse]:
    redact = user.role == UserRole.viewer
    return [_to_response(w, redact_backup_error=redact) for w in session.exec(select(World)).all()]


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

    return _to_response(world, redact_backup_error=False)


@router.post("/{name}/restart", dependencies=[Depends(require_operator)])
def restart_world(
    name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    settings: Settings = Depends(settings_dependency),
) -> dict[str, str]:
    """Restarts a world's container so it picks up config pushed by
    PATCH /{name} (new/changed plugins, datapacks, or server.properties)
    — folia-nexa-node only re-syncs that config at its own startup.

    Also re-pushes this world's own LXD instance config (rcon-password
    and everything else in scheduler._node_config) right before
    restarting, backfilling rcon_password first if it's still unset —
    confirmed the hard way: a world whose PATCH-time config push failed
    (or predates a field like rcon_password entirely) used to stay
    silently out of sync forever, since restart previously just cycled
    the container trusting LXD's config was already correct. Restart is
    now a second, independent chance for that config to actually land,
    not just a process cycle.
    """
    world, host = _host_and_world(session, name)
    _ensure_rcon_password(world)
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    session.refresh(world)

    try:
        lxd_client.update_config(host, world.container_name, _node_config(session, world, settings))
    except LXDError:
        logger.exception(
            "world '%s' config failed to push before restart — restarting anyway with "
            "whatever config the container already has",
            name,
        )

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
    settings: Settings = Depends(settings_dependency),
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
        finalize_provisioning(session, lxd_client, world, settings)
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
    draining_snapshot = _to_response(world, redact_backup_error=False)

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
    return _to_response(still_present, redact_backup_error=False)


def _host_and_world(session: Session, name: str) -> tuple[World, Host]:
    world = _get_world_or_404(session, name)
    if not world.host_name or not world.container_name:
        raise HTTPException(status.HTTP_409_CONFLICT, f"world '{name}' is not placed on a host yet")
    host = session.exec(select(Host).where(Host.name == world.host_name)).first()
    if host is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"world '{name}' points at unknown host")
    return world, host


def _require_lxd_snapshot_backups_enabled(settings: Settings) -> None:
    """The ad-hoc LXD-instance-snapshot endpoints below are kept as a
    feature (Settings.lxd_snapshot_backups_enabled's own docstring has
    the full rationale) but off by default — the tracked "time machine"
    backup feature (create_manual_backup/restore_backup below) never
    depends on this flag at all, it always uses the file-level
    world_backups.py path regardless of its setting. LXDClient itself
    separately, unconditionally refuses a "dir"-backed instance no matter
    what this flag says (LXDClient.get_storage_driver_for_instance) —
    this check is purely the "feature is off by default" toggle, not the
    safety net."""
    if not settings.lxd_snapshot_backups_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "LXD snapshot backups are disabled (Settings.lxd_snapshot_backups_enabled) — "
            "use the tracked world backup feature instead (POST /{name}/backups/manual)",
        )


def _snapshot_or_502(lxd_client: LXDClient, host: Host, world: World, snapshot_name: str) -> None:
    try:
        lxd_client.snapshot_container(host, world.container_name, snapshot_name)
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/{name}/snapshot", dependencies=[Depends(require_operator)])
def snapshot_world(
    name: str,
    snapshot_name: str | None = None,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    settings: Settings = Depends(settings_dependency),
) -> dict[str, str]:
    _require_lxd_snapshot_backups_enabled(settings)
    world, host = _host_and_world(session, name)
    label = snapshot_name or f"manual-{epoch_seconds(utcnow())}"
    _snapshot_or_502(lxd_client, host, world, label)
    return {"snapshot": label}


def _restore_snapshot_or_502(lxd_client: LXDClient, host: Host, world: World, snapshot_name: str) -> None:
    try:
        lxd_client.restore_snapshot(host, world.container_name, snapshot_name)
    except LXDError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/{name}/restore/{snapshot_name}", dependencies=[Depends(require_operator)])
def restore_world(
    name: str,
    snapshot_name: str,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    settings: Settings = Depends(settings_dependency),
) -> dict[str, str]:
    _require_lxd_snapshot_backups_enabled(settings)
    world, host = _host_and_world(session, name)
    _restore_snapshot_or_502(lxd_client, host, world, snapshot_name)
    return {"restored": snapshot_name}


class BackupResponse(BaseModel):
    id: int
    snapshot_name: str
    kind: str
    created_at: datetime
    size_bytes: int | None = None


@router.get("/{name}/backups", response_model=list[BackupResponse], dependencies=[Depends(require_viewer)])
def list_backups(name: str, session: Session = Depends(get_session)) -> list[BackupResponse]:
    """Every tracked backup for this world, newest first — the automatic
    hourly snapshots from scheduler.run_scheduled_backups, pruned to the
    last week by scheduler.prune_expired_backups. Backs the dashboard's
    "time machine" restore list."""
    _get_world_or_404(session, name)
    rows = session.exec(
        select(WorldBackup).where(WorldBackup.world_name == name).order_by(WorldBackup.created_at.desc())
    ).all()
    return [
        BackupResponse(
            id=row.id, snapshot_name=row.snapshot_name, kind=row.kind,
            created_at=row.created_at, size_bytes=row.size_bytes,
        )
        for row in rows
    ]


class BackupConfigRequest(BaseModel):
    enabled: bool


@router.put("/{name}/backups-config", dependencies=[Depends(require_operator)])
def put_backup_config(
    name: str, body: BackupConfigRequest, session: Session = Depends(get_session)
) -> dict[str, bool]:
    """Enables/disables the automatic hourly backup for this world (on by
    default — World.backups_enabled). Disabling only stops *future*
    scheduled backups; it doesn't delete backups already taken, and those
    still expire on their normal week-long schedule via
    prune_expired_backups.

    Disabling also clears last_backup_error/last_backup_attempt_at:
    run_scheduled_backups only ever visits backups_enabled worlds, so
    without this a failure banner from before the operator disabled
    backups would otherwise be stuck on the dashboard forever, never
    reaching the success path that normally clears it."""
    world = _get_world_or_404(session, name)
    world.backups_enabled = body.enabled
    if not body.enabled:
        world.last_backup_error = None
        world.last_backup_attempt_at = None
    world.updated_at = utcnow()
    session.add(world)
    session.commit()
    return {"backups_enabled": world.backups_enabled}


def _backup_or_502(settings: Settings, world: World, label: str) -> int:
    try:
        return world_backups.fetch_and_store_backup(settings, world, label)
    except world_backups.BackupTransferError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/{name}/backups/manual", response_model=BackupResponse, dependencies=[Depends(require_operator)])
def create_manual_backup(
    name: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dependency),
) -> BackupResponse:
    """Takes a backup right now, independent of the hourly schedule (e.g.
    right before a risky plugin upgrade) — operator-gated like the older
    ad-hoc POST /{name}/snapshot, but tracked as a WorldBackup row (kind=
    "manual") so it shows up in, and can be restored from, the same
    dashboard list as scheduled backups, and expires on the same
    BACKUP_RETENTION schedule as prune_expired_backups. Works regardless
    of World.backups_enabled — that flag only gates the automatic hourly
    schedule, not an operator's own explicit request. Streams a tar.gz of
    the world save + plugins/ off the world's own node agent
    (world_backups.fetch_and_store_backup) — doesn't touch LXD at all, so
    it works the same regardless of the host's storage pool driver.

    The label is manual-backup-<epoch>-<random>, not plain manual-<epoch>
    — that format is already used by the older ad-hoc POST /{name}/snapshot
    (off by default, see Settings.lxd_snapshot_backups_enabled), and the
    random suffix also protects against two calls to this endpoint itself
    landing in the same second (e.g. a double-clicked "Back up now"
    button)."""
    world = _get_world_or_404(session, name)
    if not world.address:
        raise HTTPException(status.HTTP_409_CONFLICT, f"world '{name}' is not placed on a host yet")
    now = utcnow()
    label = f"manual-backup-{epoch_seconds(now)}-{secrets.token_hex(3)}"
    size_bytes = _backup_or_502(settings, world, label)
    backup = WorldBackup(world_name=name, snapshot_name=label, kind="manual", created_at=now, size_bytes=size_bytes)
    session.add(backup)
    session.commit()
    session.refresh(backup)
    return BackupResponse(
        id=backup.id, snapshot_name=backup.snapshot_name, kind=backup.kind,
        created_at=backup.created_at, size_bytes=backup.size_bytes,
    )


@router.post("/{name}/backups/{backup_id}/restore", dependencies=[Depends(require_admin)])
def restore_backup(
    name: str,
    backup_id: int,
    session: Session = Depends(get_session),
    lxd_client: LXDClient = Depends(get_lxd_client),
    settings: Settings = Depends(settings_dependency),
) -> dict[str, str]:
    """"Time machine" restore: rolls this world back to one of its tracked
    backups (GET /{name}/backups). Admin-only — stricter than the plain
    operator-gated POST /{name}/restore/{snapshot_name} above, since this
    is reachable straight from the dashboard by anyone with a login,
    rather than requiring CLI/API access to an exact snapshot name.

    Pushes the stored tar.gz to {NODE_WORLD_DIR}/.pending-restore.tar.gz
    (proven against a *running* container — every push_file call site in
    this codebase already targets a live world, same as this one) and
    restarts the container — the same restart_container call
    scheduler.recover_crashed_worlds already relies on, which fully
    cycles the node agent process (it's the container's PID 1), so its
    main() runs from scratch and picks up the marker before any other
    staging (node/src/folia_node/agent.py). Sets phase back to
    provisioning, mirroring exactly what recover_crashed_worlds does
    after its own restart_container call — the existing reconcile
    machinery (finalize_provisioning) confirms the world comes back
    healthy from there, no new polling logic needed. Restoring onto a
    *different* host works the same way with no special-casing: this
    endpoint only ever looks at wherever `world.host_name`/`container_name`
    currently point, not the host the backup was originally taken on —
    place a same-named world on a healthy host first (or migrate an
    existing one there), then call this.

    The whole push+restart sequence runs under lxd_client.restore_guard,
    which 409s immediately (before touching the network) if a restore of
    this exact world is already in flight — a double-clicked dashboard
    Restore button, or two admins restoring the same world at once,
    would otherwise race two pushes of the same marker file and two
    restart_container calls against the same container. The tarball
    itself is streamed off disk (world_backups.iter_backup_file) rather
    than loaded fully into mgmt's memory first, and both the push and
    the restart get a widened client-side wait — see LONG_OPERATION_TIMEOUT/
    RESTART_WAIT_TIMEOUT's own comments in lxd_client.py — since a
    multi-GB world+plugins tarball, and the restart that follows it, can
    legitimately take longer than the 15s default this class otherwise
    uses everywhere else."""
    world, host = _host_and_world(session, name)
    backup = session.get(WorldBackup, backup_id)
    if backup is None or backup.world_name != name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such backup '{backup_id}' for world '{name}'")

    backup_path = world_backups.backup_file_path(settings, name, backup.snapshot_name)
    if not backup_path.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"backup '{backup_id}' for world '{name}' has no tarball on this mgmt host's disk "
            f"(expected {backup_path})",
        )
    marker_path = f"{NODE_WORLD_DIR}/.pending-restore.tar.gz"

    try:
        with lxd_client.restore_guard(host, world.container_name):
            try:
                lxd_client.push_file(
                    host,
                    world.container_name,
                    marker_path,
                    world_backups.iter_backup_file(backup_path),
                    timeout=LONG_OPERATION_TIMEOUT,
                )
            except LXDError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

            try:
                lxd_client.restart_container(host, world.container_name, timeout=RESTART_WAIT_TIMEOUT)
            except LXDError as exc:
                # The marker is now armed but the restart that was supposed to
                # consume it (node/src/folia_node/agent.py's
                # _apply_pending_restore, on the next process start) never
                # happened — left as-is, it would silently fire on some later
                # unrelated restart with no operator awareness. Best-effort
                # remove it so this restore attempt fails cleanly instead.
                try:
                    lxd_client.delete_file(host, world.container_name, marker_path)
                except LXDError:
                    logger.exception(
                        "restore of world '%s' failed to restart AND failed to clean up its pending-restore "
                        "marker at %s — it may still be applied on a later unrelated restart",
                        name,
                        marker_path,
                    )
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except RestoreInProgressError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    world.phase = WorldPhase.provisioning
    # Cleared here, not left showing whatever the *previous* restore's
    # outcome was — finalize_provisioning's _record_restore_outcome sets
    # the real value for *this* attempt once the container's back up and
    # the node agent reports in.
    world.last_restore_error = None
    world.last_restore_confirmed_at = None
    session.add(world)
    session.commit()
    return {"restoring": backup.snapshot_name, "created_at": backup.created_at.isoformat()}


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

    return _to_response(world, redact_backup_error=False)


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
