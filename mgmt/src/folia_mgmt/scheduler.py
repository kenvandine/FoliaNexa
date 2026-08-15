"""Placement + reconcile loop. PLAN.md §5.

`reconcile()` is a plain function over a Session + LXDClient (+ a health
check callable) so it can be called directly from a request handler
(best-effort immediate placement), from tests (with fakes for both), or
from the background loop started at app startup — one implementation,
three callers.
"""

from __future__ import annotations

import logging
from typing import Callable

import httpx
from sqlmodel import Session, select

from folia_mgmt.access_apply import apply_whitelist
from folia_mgmt.config import Settings, get_settings
from folia_mgmt.lxd_client import LXDClient, LXDError, extract_ipv4
from folia_mgmt.models import Host, HostStatus, World, WorldPhase

logger = logging.getLogger(__name__)

# Base image with folia-smp-node preinstalled. PLAN.md §17 Phase 2 step 3.
DEFAULT_IMAGE_ALIAS = "folia-node-base"

# Every world's node agent listens on the standard Minecraft port; PLAN.md
# doesn't (yet) support per-world port overrides.
MINECRAFT_PORT = 25565

HealthCheck = Callable[[World, Settings], bool]


def _allocated(session: Session, host_name: str) -> tuple[int, int]:
    placed = session.exec(
        select(World).where(
            World.host_name == host_name,
            World.phase.in_([WorldPhase.provisioning, WorldPhase.running]),
        )
    ).all()
    return sum(w.cpu_cores for w in placed), sum(w.memory_gb for w in placed)


def _labels_match(host: Host, world: World) -> bool:
    return all(host.labels.get(k) == v for k, v in world.placement_labels.items())


def select_host(session: Session, world: World) -> Host | None:
    """Fullest-fit-remaining bin-packing: PLAN.md §5 step 3 — pick the host
    with the most free capacity left *after* placement, spreading load
    rather than stacking everything on the first host that fits."""
    best: Host | None = None
    best_remaining = -1

    for host in session.exec(select(Host).where(Host.status == HostStatus.online)).all():
        if not _labels_match(host, world):
            continue
        used_cpu, used_mem = _allocated(session, host.name)
        free_cpu = host.capacity_cpu_cores - used_cpu
        free_mem = host.capacity_memory_gb - used_mem
        if free_cpu < world.cpu_cores or free_mem < world.memory_gb:
            continue
        remaining = (free_cpu - world.cpu_cores) + (free_mem - world.memory_gb)
        if remaining > best_remaining:
            best_remaining = remaining
            best = host

    return best


def _node_config(world: World, settings: Settings) -> dict[str, str]:
    """`user.folia.*` instance config — this is what folia-smp-node reads
    off the devlxd socket to learn its assignment. PLAN.md §9."""
    base = settings.artifacts_base_url.rstrip("/")
    config = {
        "user.folia.world-name": world.name,
        "user.folia.world-type": world.type.value,
        "user.folia.jar-engine": world.engine,
        "user.folia.jar-version": world.version,
        "user.folia.jar-url": f"{base}/{world.engine}/{world.version}/{world.engine}.jar",
    }
    if world.plugins:
        config["user.folia.plugins-manifest-url"] = f"{base}/{world.engine}/manifests/{world.name}.json"
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
            config=_node_config(world, settings),
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
    if world.host_name and world.container_name:
        host = session.exec(select(Host).where(Host.name == world.host_name)).first()
        if host is not None:
            try:
                lxd_client.delete_container(host, world.container_name)
            except LXDError:
                logger.exception("failed to delete container for world '%s', will retry", world.name)
                return
    world.phase = WorldPhase.deleted
    session.add(world)
    session.commit()


def default_health_check(world: World, settings: Settings) -> bool:
    """Polls folia-smp-node's /healthz (PLAN.md §9) directly by IP — mgmt
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


def reconcile(
    session: Session,
    lxd_client: LXDClient,
    settings: Settings | None = None,
    health_check: HealthCheck | None = None,
) -> None:
    settings = settings or get_settings()
    health_check = health_check or default_health_check

    for world in session.exec(select(World).where(World.phase == WorldPhase.pending)).all():
        place_world(session, lxd_client, world, settings)

    for world in session.exec(select(World).where(World.phase == WorldPhase.provisioning)).all():
        finalize_provisioning(session, lxd_client, world)

    check_running_worlds(session, settings, health_check)
    recover_crashed_worlds(session, lxd_client)
    sync_whitelisted_worlds(session, lxd_client)

    for world in session.exec(select(World).where(World.phase == WorldPhase.draining)).all():
        teardown_world(session, lxd_client, world)
