"""Placement + reconcile loop. PLAN.md §5.

`reconcile()` is a plain function over a Session + LXDClient (+ a health
check callable) so it can be called directly from a request handler
(best-effort immediate placement), from tests (with fakes for both), or
from the background loop started at app startup — one implementation,
three callers.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable

import httpx
from sqlmodel import Session, select

from folia_mgmt.access_apply import apply_whitelist
from folia_mgmt.config import Settings, get_settings
from folia_mgmt.folianexa_stats import apply_stats_config
from folia_mgmt.luckperms import apply_luckperms_config
from folia_mgmt.lxd_client import LXDClient, LXDError, extract_ipv4
from folia_mgmt.models import (
    Host,
    HostStatus,
    MinecraftVersionConfig,
    World,
    WorldBackup,
    WorldPhase,
    WorldPluginConfigFile,
    utcnow,
)
from folia_mgmt.plugin_files import MANAGED_PLUGIN_IDS, plugin_root

# World backups (PLAN.md — dashboard "time machine" restores): an hourly
# LXD snapshot of every backups_enabled running world, kept for a week.
BACKUP_INTERVAL = timedelta(hours=1)
BACKUP_RETENTION = timedelta(days=7)

logger = logging.getLogger(__name__)

# Base image with folia-nexa-node preinstalled. PLAN.md §17 Phase 2 step 3.
DEFAULT_IMAGE_ALIAS = "folia-node-base"

# Every world's node agent listens on the standard Minecraft port; PLAN.md
# doesn't (yet) support per-world port overrides.
MINECRAFT_PORT = 25565

HealthCheck = Callable[[World, Settings], bool]


def allocated_capacity(session: Session, host_name: str) -> tuple[int, int]:
    placed = session.exec(
        select(World).where(
            World.host_name == host_name,
            World.phase.in_([WorldPhase.provisioning, WorldPhase.running]),
        )
    ).all()
    return sum(w.cpu_cores for w in placed), sum(w.memory_gb for w in placed)


def _labels_match(host: Host, world: World) -> bool:
    return all(host.labels.get(k) == v for k, v in world.placement_labels.items())


def check_host_health(session: Session, lxd_client: LXDClient) -> None:
    """Periodic reachability check for every trusted host — until now
    `Host.status` was only ever written at enrollment (`online`,
    routers/hosts.py's enroll_host) or by an operator's explicit drain
    (`draining`); nothing ever re-verified a host afterward, so one that
    lost power or network stayed `online` in the DB (and dashboard)
    forever. Only online/offline are touched here — draining/cordoned are
    operator-managed states this must never override, so they're excluded
    from the query entirely.

    Single-check like check_running_worlds (no debounce/consecutive-
    failure counter) — consistent with how this codebase already treats a
    failed reachability probe elsewhere in the reconcile loop.
    """
    hosts = session.exec(select(Host).where(Host.status.in_([HostStatus.online, HostStatus.offline]))).all()
    for host in hosts:
        reachable = lxd_client.ping_host(host)
        if reachable and host.status != HostStatus.online:
            logger.info("host '%s' reachable again, marking online", host.name)
            host.status = HostStatus.online
            session.add(host)
            session.commit()
        elif not reachable and host.status != HostStatus.offline:
            logger.warning("host '%s' failed reachability check, marking offline", host.name)
            host.status = HostStatus.offline
            session.add(host)
            session.commit()


def select_host(session: Session, world: World, exclude: str | None = None) -> Host | None:
    """Fullest-fit-remaining bin-packing: PLAN.md §5 step 3 — pick the host
    with the most free capacity left *after* placement, spreading load
    rather than stacking everything on the first host that fits.

    `exclude` is a host name to skip regardless of its status — used by
    migrate_worlds_off_draining_hosts to pick a *different* host for a
    world currently sitting on the draining one being evacuated."""
    best: Host | None = None
    best_remaining = -1

    for host in session.exec(select(Host).where(Host.status == HostStatus.online)).all():
        if host.name == exclude:
            continue
        if not _labels_match(host, world):
            continue
        used_cpu, used_mem = allocated_capacity(session, host.name)
        free_cpu = host.capacity_cpu_cores - used_cpu
        free_mem = host.capacity_memory_gb - used_mem
        if free_cpu < world.cpu_cores or free_mem < world.memory_gb:
            continue
        remaining = (free_cpu - world.cpu_cores) + (free_mem - world.memory_gb)
        if remaining > best_remaining:
            best_remaining = remaining
            best = host

    return best


def _node_config(session: Session, world: World, settings: Settings) -> dict[str, str]:
    """`user.folia.*` instance config — this is what folia-nexa-node reads
    off the devlxd socket to learn its assignment. PLAN.md §9.

    jar-engine/jar-version/jar-url come from the cluster-wide
    MinecraftVersionConfig singleton, not World.engine/World.version —
    see that model's own docstring for why every world has to run the
    same version regardless of what created it declared. jar-url itself
    still points at an external artifacts host (the engine jar is real
    binary weight mgmt shouldn't be in the business of serving).
    plugins-manifest-url comes from mgmt's own API instead — it's
    generated live from the plugin catalog (PLAN.md §14), so there's no
    hand-authored manifest file to keep in sync with a world's plugins
    list anymore. World creation already validated plugins against the
    catalog and that public_url is set (routers/worlds.py), so this can
    assume both are fine by the time a world reaches placement.
    datapacks-manifest-url is the same pattern again, one catalog over
    (datapack_catalog.py) — see routers/worlds.py's get_datapacks_manifest
    and folia_node.staging for where node stages the result.
    server-properties-manifest-url is the same pattern a third time, for
    World.properties — see get_server_properties_manifest.

    All three manifest URLs are set whenever public_url is configured,
    regardless of whether the underlying list/dict is currently empty
    (each manifest endpoint just returns an empty result in that case) —
    that keeps the URL itself stable across a world's lifetime, so
    folia_node.staging can always re-fetch it on every agent restart to
    pick up a later PATCH /worlds/{name} edit, rather than needing a
    config push just because a previously-empty list gained its first
    entry. A world *placed* before this manifest existed at all still
    needs one push_config call to pick up the key in the first place —
    see routers/worlds.py's update_world.
    """
    mc = session.get(MinecraftVersionConfig, 1) or MinecraftVersionConfig()
    base = settings.artifacts_base_url.rstrip("/")
    config = {
        "user.folia.world-name": world.name,
        "user.folia.world-type": world.type.value,
        "user.folia.jar-engine": mc.engine,
        "user.folia.jar-version": mc.version,
        "user.folia.jar-url": f"{base}/{mc.engine}/{mc.version}/{mc.engine}.jar",
        # Every world needs this, not just ones behind a specific proxy —
        # folia-nexa-node writes it into config/paper-global.yml so this
        # backend trusts identity forwarded by folia-nexa-proxy's Velocity
        # "modern" forwarding (PLAN.md §7, routers/routes.py's matching
        # /forwarding-secret endpoint the proxy itself polls).
        "user.folia.velocity-forwarding-secret": settings.get_velocity_forwarding_secret(),
    }
    if world.rcon_password:
        # See World.rcon_password's own comment for why this rides along
        # here instead of server-properties-manifest — folia_node.staging
        # turns this into enable-rcon/rcon.port/rcon.password in
        # server.properties, required-baseline style, never operator-
        # settable (routers/worlds.py's _PROTECTED_PROPERTIES).
        config["user.folia.rcon-password"] = world.rcon_password
    if settings.public_url:
        public_url = settings.public_url.rstrip("/")
        config["user.folia.plugins-manifest-url"] = f"{public_url}/api/v1/worlds/{world.name}/plugins-manifest"
        config["user.folia.datapacks-manifest-url"] = f"{public_url}/api/v1/worlds/{world.name}/datapacks-manifest"
        config["user.folia.server-properties-manifest-url"] = (
            f"{public_url}/api/v1/worlds/{world.name}/server-properties-manifest"
        )
    return config


def place_world(
    session: Session, lxd_client: LXDClient, world: World, settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    host: Host | None = None
    if world.sticky_host:
        host = session.exec(select(Host).where(Host.name == world.sticky_host)).first()
        if host is not None and host.status != HostStatus.online:
            host = None  # sticky host unavailable — stay pending rather than reschedule elsewhere
    if host is None and not world.sticky_host:
        host = select_host(session, world)
    if host is None:
        return  # no capacity yet; stays pending, next reconcile tick retries

    container_name = world.name
    try:
        lxd_client.launch_container(
            host,
            container_name,
            DEFAULT_IMAGE_ALIAS,
            cpu_cores=world.cpu_cores,
            memory_gb=world.memory_gb,
            config=_node_config(session, world, settings),
            snapshot_schedule=world.snapshot_schedule,
            snapshot_expiry=world.snapshot_expiry,
        )
    except LXDError:
        logger.exception("failed to launch world '%s' on host '%s'", world.name, host.name)
        return  # stays pending; next tick retries (possibly against a different host)

    world.host_name = host.name
    world.container_name = container_name
    world.sticky_host = host.name
    world.phase = WorldPhase.provisioning
    session.add(world)
    session.commit()


def finalize_provisioning(session: Session, lxd_client: LXDClient, world: World) -> None:
    """A provisioning world becomes 'running' once LXD reports it has an
    address — that's the extent of health-checking until §9's node agent
    exposes /healthz for mgmt to poll instead (tracked as a follow-up)."""
    host = session.exec(select(Host).where(Host.name == world.host_name)).first()
    if host is None or not world.container_name:
        return
    try:
        state = lxd_client.get_instance_state(host, world.container_name)
    except LXDError:
        logger.exception("failed to poll state for world '%s'", world.name)
        return

    ip = extract_ipv4(state)
    if ip is None:
        return  # DHCP lease not up yet; next tick retries

    world.address = f"{ip}:{MINECRAFT_PORT}"
    world.phase = WorldPhase.running
    session.add(world)
    session.commit()


def teardown_world(session: Session, lxd_client: LXDClient, world: World) -> None:
    """Hard-deletes the row once its container (if it had one) is
    actually gone — not a soft delete. World.name is a real unique DB
    column, so leaving a phase=deleted row around forever would
    permanently block an operator from ever reusing that world name.
    Nothing else in this codebase queries WorldPhase.deleted rows
    (allocated_capacity/select_host only care about *live* worlds, and
    are written to tolerate a stray deleted-phase row if one ever exists
    from an older DB, but don't rely on one existing) — the enum value
    itself stays, since a container mid-teardown that fails here (LXD
    unreachable) still needs *some* transient state, but success means
    genuinely gone, not parked forever.
    """
    if world.host_name and world.container_name:
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is not None:
            try:
                lxd_client.delete_container(host, world.container_name)
            except LXDError:
                logger.exception("failed to delete container for world '%s', will retry", world.name)
                return
    session.delete(world)
    session.commit()


def default_health_check(world: World, settings: Settings) -> bool:
    """Polls folia-nexa-node's /healthz (PLAN.md §9) directly by IP — mgmt
    already has network reachability to every world it placed."""
    if not world.address:
        return False
    host = world.address.split(":", 1)[0]
    url = f"http://{host}:{settings.node_health_port}/healthz"
    try:
        resp = httpx.get(url, timeout=settings.node_health_timeout_seconds)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def check_running_worlds(session: Session, settings: Settings, health_check: HealthCheck) -> None:
    """PLAN.md §5: 'crashed: node agent's local health endpoint stops
    responding ... mgmt restarts the container.'  This half detects it;
    `recover_crashed_worlds` does the restart."""
    for world in session.exec(select(World).where(World.phase == WorldPhase.running)).all():
        if not health_check(world, settings):
            logger.warning("world '%s' failed its health check, marking crashed", world.name)
            world.phase = WorldPhase.crashed
            session.add(world)
            session.commit()


def migrate_worlds_off_draining_hosts(session: Session, lxd_client: LXDClient) -> None:
    """`POST /hosts/{name}/drain` (routers/hosts.py) used to only stop new
    placements from landing on a host — worlds already there just kept
    running there forever, with no automatic evacuation, contradicting
    PLAN.md §2's documented "host maintenance/decommission" intent for
    the status. This is the fix: every reconcile pass, any world still
    sitting on a `draining` host gets moved to whichever `online` host
    currently has the most free capacity after the move, via the same
    stop/export/import/start path as the manual per-world `POST
    /worlds/{name}/migrate` endpoint (PLAN.md §13 — a brief outage is
    expected, this isn't live migration).

    Runs before check_running_worlds/recover_crashed_worlds in reconcile()
    so a world (including a crashed one) leaves a draining host by being
    migrated, not by being health-checked or restarted in place — a
    restart wouldn't get it off the host the operator is trying to empty.

    A world that can't currently be moved (no online host with room, or
    the drain target's labels don't match) is left in place and retried
    next tick, same "stays put" convention as place_world for a pending
    world with nowhere to go yet.
    """
    worlds = session.exec(
        select(World).where(World.phase.in_([WorldPhase.running, WorldPhase.provisioning, WorldPhase.crashed]))
    ).all()
    for world in worlds:
        if not world.host_name or not world.container_name:
            continue
        source_host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if source_host is None or source_host.status != HostStatus.draining:
            continue

        target = select_host(session, world, exclude=source_host.name)
        if target is None:
            logger.warning(
                "world '%s' stuck on draining host '%s' — no online host with capacity to migrate to yet",
                world.name,
                source_host.name,
            )
            continue

        try:
            lxd_client.migrate_container(source_host, world.container_name, target)
        except LXDError:
            logger.exception(
                "failed to migrate world '%s' off draining host '%s', will retry next tick",
                world.name,
                source_host.name,
            )
            continue

        world.host_name = target.name
        world.sticky_host = target.name
        world.phase = WorldPhase.provisioning
        world.address = None
        world.updated_at = utcnow()
        session.add(world)
        session.commit()
        logger.info("migrated world '%s' off draining host '%s' to '%s'", world.name, source_host.name, target.name)

        try:
            lxd_client.delete_container(source_host, world.container_name)
        except LXDError:
            logger.exception(
                "migrated world '%s' off '%s' but failed to delete the old container there — clean up manually",
                world.name,
                source_host.name,
            )


def recover_crashed_worlds(session: Session, lxd_client: LXDClient) -> None:
    for world in session.exec(select(World).where(World.phase == WorldPhase.crashed)).all():
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is None or not world.container_name:
            continue
        try:
            lxd_client.restart_container(host, world.container_name)
        except LXDError:
            logger.exception("failed to restart crashed world '%s', will retry next tick", world.name)
            continue
        logger.info("restarted container for crashed world '%s'", world.name)
        # Not a fresh reschedule — same host/container/data, just a
        # restart. finalize_provisioning re-polls for an address and
        # flips it back to running once the JVM is back up.
        world.phase = WorldPhase.provisioning
        session.add(world)
        session.commit()


def sync_whitelisted_worlds(session: Session, lxd_client: LXDClient) -> None:
    """Keeps whitelist.json current on every whitelist-enabled running
    world as Discord approvals (§11C) come and go — this is the periodic
    catch-up; `PUT /worlds/{name}/access` also applies it immediately when
    the toggle itself changes. See access_apply.py's module docstring for
    what "whitelist_enabled" actually means here."""
    worlds = session.exec(
        select(World).where(World.phase == WorldPhase.running, World.whitelist_enabled.is_(True))
    ).all()
    for world in worlds:
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is not None:
            apply_whitelist(session, lxd_client, host, world)


def sync_luckperms_configs(session: Session, lxd_client: LXDClient, settings: Settings) -> None:
    """Keeps every LuckPerms-enabled running world's config.yml pointed at
    the shared MySQL backend (PLAN.md §11B) — no-op if that backend isn't
    configured. See luckperms.py's module docstring for what this does
    and doesn't automate."""
    if not settings.luckperms_configured:
        return
    worlds = session.exec(select(World).where(World.phase == WorldPhase.running)).all()
    for world in worlds:
        if "LuckPerms" not in world.plugins:
            continue
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is not None:
            apply_luckperms_config(lxd_client, host, world, settings)


def sync_stats_configs(session: Session, lxd_client: LXDClient, settings: Settings) -> None:
    """Keeps every FoliaNexaStats-enabled running world's config.yml
    pointed at a real mgmt API token, so its periodic stats reports
    don't 401 — see folianexa_stats.py's module docstring."""
    worlds = session.exec(select(World).where(World.phase == WorldPhase.running)).all()
    for world in worlds:
        if "FoliaNexaStats" not in world.plugins:
            continue
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is not None:
            apply_stats_config(session, lxd_client, host, world, settings)


def sync_plugin_config_files(session: Session, lxd_client: LXDClient) -> None:
    """Keeps every running world's operator-edited plugin config file
    overrides (mgmt/src/folia_mgmt/routers/plugin_config.py) pushed to
    their container — same "overwrite unconditionally every tick, swallow
    LXDError and retry next time" pattern as sync_luckperms_configs above,
    generalized to any plugin instead of one hardcoded integration."""
    rows = session.exec(select(WorldPluginConfigFile)).all()
    if not rows:
        return

    by_world: dict[str, list[WorldPluginConfigFile]] = {}
    for row in rows:
        by_world.setdefault(row.world_name, []).append(row)

    for world_name, files in by_world.items():
        world = session.exec(select(World).where(World.name == world_name)).first()
        if world is None or world.phase != WorldPhase.running or not world.host_name or not world.container_name:
            continue
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is None:
            continue
        for row in files:
            # A plugin removed from the world's declared list is no longer
            # ours to manage — stop re-creating its config file forever.
            # The read/write endpoints already 404 on this via
            # _require_declared_plugin; this is the reconcile-loop half of
            # the same rule.
            if row.plugin_id not in world.plugins:
                continue
            # Defense in depth: put_file already refuses to create a row
            # for a managed plugin (routers/plugin_config.py's
            # _require_not_managed), but a row could still exist from
            # before that check shipped. Skip it rather than push it —
            # pushing here would silently and permanently clobber
            # sync_luckperms_configs'/sync_stats_configs' own config on
            # every tick, since this runs after both of them below.
            if row.plugin_id in MANAGED_PLUGIN_IDS:
                continue
            try:
                lxd_client.push_file(host, world.container_name, f"{plugin_root(row.plugin_id)}/{row.path}", row.content)
            except LXDError:
                logger.debug(
                    "failed to push plugin config '%s' for '%s' on world '%s', will retry next reconcile",
                    row.path, row.plugin_id, world_name,
                )


def run_scheduled_backups(session: Session, lxd_client: LXDClient) -> None:
    """Takes an hourly LXD snapshot of every backups_enabled running world,
    tracked in WorldBackup so the dashboard can list backups and restore
    from one ("time machine" style) — unlike the older ad-hoc POST
    /worlds/{name}/snapshot, which creates a raw LXD snapshot mgmt never
    records anywhere. reconcile() itself runs every 15s (see
    RECONCILE_INTERVAL_SECONDS in main.py), so this self-gates to hourly by
    comparing "now" against each world's own most recent scheduled-backup
    row rather than firing every tick."""
    worlds = session.exec(
        select(World).where(World.phase == WorldPhase.running, World.backups_enabled.is_(True))
    ).all()
    now = utcnow()
    for world in worlds:
        latest = session.exec(
            select(WorldBackup)
            .where(WorldBackup.world_name == world.name, WorldBackup.kind == "scheduled")
            .order_by(WorldBackup.created_at.desc())
        ).first()
        if latest is not None and now - latest.created_at < BACKUP_INTERVAL:
            continue
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is None or not world.container_name:
            continue
        label = f"auto-{int(now.timestamp())}"
        try:
            lxd_client.snapshot_container(host, world.container_name, label)
        except LXDError:
            logger.exception("scheduled backup of world '%s' failed, will retry next reconcile", world.name)
            continue
        session.add(WorldBackup(world_name=world.name, snapshot_name=label, kind="scheduled", created_at=now))
        session.commit()


def prune_expired_backups(session: Session, lxd_client: LXDClient) -> None:
    """Keeps a week's worth of tracked backups (BACKUP_RETENTION) —
    deletes both the underlying LXD snapshot and its WorldBackup row once
    a backup is older than that. A snapshot-delete failure (e.g. the
    world's host is unreachable right now) leaves the row in place so it's
    retried on a later reconcile tick, rather than mgmt losing track of a
    snapshot that's still actually sitting on the host's storage pool."""
    cutoff = utcnow() - BACKUP_RETENTION
    expired = session.exec(select(WorldBackup).where(WorldBackup.created_at < cutoff)).all()
    for backup in expired:
        world = session.exec(select(World).where(World.name == backup.world_name)).first()
        if world is not None and world.host_name and world.container_name:
            host = session.exec(select(Host).where(Host.name == world.host_name)).first()
            if host is not None:
                try:
                    lxd_client.delete_snapshot(host, world.container_name, backup.snapshot_name)
                except LXDError:
                    logger.exception(
                        "failed to delete expired snapshot '%s' for world '%s', will retry next reconcile",
                        backup.snapshot_name,
                        backup.world_name,
                    )
                    continue
        session.delete(backup)
        session.commit()


def _isolated(session: Session, step_name: str, fn: Callable[[], None]) -> None:
    """Runs one reconcile step (or one world's turn within a step's loop),
    logging and swallowing any exception instead of letting it propagate
    out of reconcile() — a single stuck host or world must not block every
    other step's turn this tick. Each step gets retried on its own the
    very next reconcile() call (15s later, see main.py's
    RECONCILE_INTERVAL_SECONDS), so skipping one tick here costs nothing
    that retry doesn't already cover.

    This bit a live deployment: one unreachable LXD host made
    recover_crashed_worlds raise uncaught (see lxd_client.py's
    _client_for docstring for the transport-vs-application-error half of
    that fix), which aborted the entire reconcile() call before it ever
    reached sync_stats_configs — stalling whitelist sync, LuckPerms config
    sync, and FoliaNexaStats token provisioning cluster-wide, for every
    world, until that one host came back.

    The rollback matters even for exceptions unrelated to lxd_client: a
    step that got partway through session.add()/commit() before failing
    leaves the session needing an explicit rollback before it's usable
    again, or the next step's own session.exec() raises
    PendingRollbackError instead of the error it's actually trying to
    report.
    """
    try:
        fn()
    except Exception:
        logger.exception("reconcile step '%s' failed, will retry next tick", step_name)
        session.rollback()


def reconcile(
    session: Session,
    lxd_client: LXDClient,
    settings: Settings | None = None,
    health_check: HealthCheck | None = None,
) -> None:
    settings = settings or get_settings()
    health_check = health_check or default_health_check

    _isolated(session, "check_host_health", lambda: check_host_health(session, lxd_client))

    for world in session.exec(select(World).where(World.phase == WorldPhase.pending)).all():
        _isolated(session, f"place_world({world.name})",
                   lambda world=world: place_world(session, lxd_client, world, settings))

    for world in session.exec(select(World).where(World.phase == WorldPhase.provisioning)).all():
        _isolated(session, f"finalize_provisioning({world.name})",
                   lambda world=world: finalize_provisioning(session, lxd_client, world))

    _isolated(session, "migrate_worlds_off_draining_hosts",
               lambda: migrate_worlds_off_draining_hosts(session, lxd_client))
    _isolated(session, "check_running_worlds", lambda: check_running_worlds(session, settings, health_check))
    _isolated(session, "recover_crashed_worlds", lambda: recover_crashed_worlds(session, lxd_client))
    _isolated(session, "sync_whitelisted_worlds", lambda: sync_whitelisted_worlds(session, lxd_client))
    _isolated(session, "sync_luckperms_configs", lambda: sync_luckperms_configs(session, lxd_client, settings))
    _isolated(session, "sync_stats_configs", lambda: sync_stats_configs(session, lxd_client, settings))
    _isolated(session, "sync_plugin_config_files", lambda: sync_plugin_config_files(session, lxd_client))
    _isolated(session, "run_scheduled_backups", lambda: run_scheduled_backups(session, lxd_client))
    _isolated(session, "prune_expired_backups", lambda: prune_expired_backups(session, lxd_client))

    for world in session.exec(select(World).where(World.phase == WorldPhase.draining)).all():
        _isolated(session, f"teardown_world({world.name})",
                   lambda world=world: teardown_world(session, lxd_client, world))
