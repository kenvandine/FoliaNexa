from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, SQLModel, create_engine, select

from folia_mgmt.config import Settings
from folia_mgmt.lxd_client import LXDError
from folia_mgmt.models import (
    AccessRequest,
    AccessRequestStatus,
    Host,
    HostStatus,
    MinecraftVersionConfig,
    World,
    WorldBackup,
    WorldPhase,
    WorldPluginConfigFile,
    WorldType,
    utcnow,
)
from folia_mgmt.scheduler import (
    BACKUP_INTERVAL,
    BACKUP_RETENTION,
    _node_config,
    check_host_health,
    check_running_worlds,
    migrate_worlds_off_draining_hosts,
    prune_expired_backups,
    recover_crashed_worlds,
    run_scheduled_backups,
    select_host,
    sync_plugin_config_files,
    sync_whitelisted_worlds,
    teardown_world,
)


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_select_host_returns_none_when_no_hosts():
    session = _session()
    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1)
    assert select_host(session, world) is None


def test_select_host_skips_offline_hosts():
    session = _session()
    session.add(Host(name="offline", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.offline))
    session.commit()

    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1)
    assert select_host(session, world) is None


def test_select_host_skips_insufficient_capacity():
    session = _session()
    session.add(Host(name="tiny", address="1.2.3.4:8443", capacity_cpu_cores=1, capacity_memory_gb=1, status=HostStatus.online))
    session.commit()

    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8)
    assert select_host(session, world) is None


def test_select_host_accounts_for_existing_placements():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=4, capacity_memory_gb=8, status=HostStatus.online))
    session.add(
        World(
            name="already-placed",
            type=WorldType.nether,
            cpu_cores=3,
            memory_gb=6,
            host_name="node-a",
            phase=WorldPhase.running,
        )
    )
    session.commit()

    # only 1 cpu / 2 gb left on node-a; this world needs 2 cpu
    world = World(name="new", type=WorldType.lobby, cpu_cores=2, memory_gb=1)
    assert select_host(session, world) is None


def test_select_host_ignores_deleted_worlds_capacity():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=4, capacity_memory_gb=8, status=HostStatus.online))
    session.add(
        World(
            name="torn-down",
            type=WorldType.nether,
            cpu_cores=3,
            memory_gb=6,
            host_name="node-a",
            phase=WorldPhase.deleted,
        )
    )
    session.commit()

    world = World(name="new", type=WorldType.lobby, cpu_cores=2, memory_gb=1)
    host = select_host(session, world)
    assert host is not None
    assert host.name == "node-a"


def test_select_host_honors_label_affinity():
    session = _session()
    session.add(Host(name="p-core-host", address="1.1.1.1:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online, labels={"cpu_type": "p-core"}))
    session.add(Host(name="e-core-host", address="2.2.2.2:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online, labels={"cpu_type": "e-core"}))
    session.commit()

    world = World(name="w", type=WorldType.overworld, cpu_cores=2, memory_gb=2, placement_labels={"cpu_type": "e-core"})
    host = select_host(session, world)
    assert host is not None
    assert host.name == "e-core-host"


class _RecordingLXDClient:
    def __init__(self):
        self.restarted: list[str] = []
        self.pushed: dict[tuple[str, str], bytes] = {}
        self.migrated: list[tuple[str, str, str]] = []  # (source_host, name, target_host)
        self.deleted: list[tuple[str, str]] = []
        self.fail_migrate_for: set[str] = set()
        self.fail_push_for: set[str] = set()  # paths to raise LXDError for
        self.snapshots: list[tuple[str, str, str]] = []  # (host, name, snapshot_name)
        self.deleted_snapshots: list[tuple[str, str, str]] = []
        self.fail_snapshot_for: set[str] = set()  # world/container names to raise LXDError for
        self.fail_delete_snapshot_for: set[str] = set()  # snapshot names to raise LXDError for

    def restart_container(self, host, name):
        self.restarted.append(name)

    def push_file(self, host, name, path, content, *, mode="0644"):
        if path in self.fail_push_for:
            raise LXDError(f"simulated push failure for {path}")
        self.pushed[(name, path)] = content

    def migrate_container(self, source_host, source_name, target_host):
        if source_name in self.fail_migrate_for:
            raise LXDError(f"simulated migration failure for {source_name}")
        self.migrated.append((source_host.name, source_name, target_host.name))

    def delete_container(self, host, name, *, stop_first=True):
        self.deleted.append((host.name, name))

    def snapshot_container(self, host, name, snapshot_name):
        if name in self.fail_snapshot_for:
            raise LXDError(f"simulated snapshot failure for {name}")
        self.snapshots.append((host.name, name, snapshot_name))

    def delete_snapshot(self, host, name, snapshot_name):
        if snapshot_name in self.fail_delete_snapshot_for:
            raise LXDError(f"simulated snapshot delete failure for {snapshot_name}")
        self.deleted_snapshots.append((host.name, name, snapshot_name))


def test_check_running_worlds_marks_unhealthy_worlds_crashed():
    session = _session()
    session.add(
        World(name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1, phase=WorldPhase.running, address="10.0.0.1:25565")
    )
    session.add(
        World(name="world-b", type=WorldType.nether, cpu_cores=1, memory_gb=1, phase=WorldPhase.running, address="10.0.0.2:25565")
    )
    session.commit()

    check_running_worlds(session, Settings(), health_check=lambda world, settings: world.name != "world-a")

    world_a = session.exec(select(World).where(World.name == "world-a")).first()
    world_b = session.exec(select(World).where(World.name == "world-b")).first()
    assert world_a.phase == WorldPhase.crashed
    assert world_b.phase == WorldPhase.running


def test_check_running_worlds_ignores_non_running_worlds():
    session = _session()
    session.add(World(name="world-pending", type=WorldType.lobby, cpu_cores=1, memory_gb=1, phase=WorldPhase.pending))
    session.commit()

    calls = []

    def health_check(world, settings):
        calls.append(world.name)
        return False

    check_running_worlds(session, Settings(), health_check)
    assert calls == []  # only phase=running worlds are ever checked


def test_recover_crashed_worlds_restarts_container_and_moves_to_provisioning():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-crashed",
            type=WorldType.overworld,
            cpu_cores=1,
            memory_gb=1,
            phase=WorldPhase.crashed,
            host_name="node-a",
            container_name="world-crashed",
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    recover_crashed_worlds(session, lxd)

    world = session.exec(select(World).where(World.name == "world-crashed")).first()
    assert world.phase == WorldPhase.provisioning
    assert lxd.restarted == ["world-crashed"]


def test_recover_crashed_worlds_skips_worlds_without_a_host():
    session = _session()
    session.add(World(name="world-orphaned", type=WorldType.lobby, cpu_cores=1, memory_gb=1, phase=WorldPhase.crashed))
    session.commit()

    lxd = _RecordingLXDClient()
    recover_crashed_worlds(session, lxd)  # must not raise

    world = session.exec(select(World).where(World.name == "world-orphaned")).first()
    assert world.phase == WorldPhase.crashed  # left alone, nothing to restart


def test_sync_whitelisted_worlds_pushes_approved_set():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-overworld",
            type=WorldType.overworld,
            cpu_cores=1,
            memory_gb=1,
            phase=WorldPhase.running,
            whitelist_enabled=True,
            host_name="node-a",
            container_name="world-overworld",
        )
    )
    session.add(
        AccessRequest(
            discord_user_id="1",
            discord_username="somebody",
            minecraft_username="Steve",
            minecraft_uuid="069a79f444e94726a5befca90e38aaf9",
            status=AccessRequestStatus.approved,
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    sync_whitelisted_worlds(session, lxd)

    import json

    content = lxd.pushed[("world-overworld", "/var/snap/folia-nexa-node/common/world/whitelist.json")]
    assert json.loads(content) == [{"uuid": "069a79f444e94726a5befca90e38aaf9", "name": "Steve"}]


def test_sync_whitelisted_worlds_skips_non_whitelisted_and_non_running():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-open",
            type=WorldType.overworld,
            cpu_cores=1,
            memory_gb=1,
            phase=WorldPhase.running,
            whitelist_enabled=False,
            host_name="node-a",
            container_name="world-open",
        )
    )
    session.add(
        World(
            name="world-not-running-yet",
            type=WorldType.overworld,
            cpu_cores=1,
            memory_gb=1,
            phase=WorldPhase.provisioning,
            whitelist_enabled=True,
            host_name="node-a",
            container_name="world-not-running-yet",
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    sync_whitelisted_worlds(session, lxd)

    assert lxd.pushed == {}


def test_node_config_sets_manifest_url_even_without_plugins():
    # The URL is stable regardless of current list content — see
    # _node_config's docstring — so a later PATCH adding plugins to a
    # previously plugin-less world doesn't need a fresh config push.
    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1)
    settings = Settings(public_url="https://mgmt.example:8443")
    config = _node_config(_session(), world, settings)
    assert config["user.folia.plugins-manifest-url"] == "https://mgmt.example:8443/api/v1/worlds/w/plugins-manifest"


def test_node_config_resolves_jar_from_cluster_wide_minecraft_version_not_world():
    # The actual bug this was built to fix: a world's own engine/version
    # columns are display-only now — jar-engine/jar-version/jar-url
    # always come from the cluster-wide MinecraftVersionConfig singleton,
    # regardless of what's stored on the world row itself.
    session = _session()
    session.add(MinecraftVersionConfig(id=1, engine="folia", version="26.2"))
    session.commit()
    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8, engine="folia", version="1.21.4")
    settings = Settings()
    config = _node_config(session, world, settings)
    assert config["user.folia.jar-engine"] == "folia"
    assert config["user.folia.jar-version"] == "26.2"
    assert config["user.folia.jar-url"] == f"{settings.artifacts_base_url}/folia/26.2/folia.jar"


def test_node_config_falls_back_to_default_minecraft_version_when_unset():
    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8)
    config = _node_config(_session(), world, Settings())
    assert config["user.folia.jar-version"] == MinecraftVersionConfig().version


def test_node_config_omits_manifest_url_without_public_url():
    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8, plugins=["LuckPerms"])
    settings = Settings(public_url=None)
    config = _node_config(_session(), world, settings)
    assert "user.folia.plugins-manifest-url" not in config


def test_node_config_sets_manifest_url_when_plugins_and_public_url_present():
    world = World(name="world-overworld", type=WorldType.overworld, cpu_cores=4, memory_gb=8, plugins=["LuckPerms", "Chunky"])
    settings = Settings(public_url="https://mgmt.example:8443/")
    config = _node_config(_session(), world, settings)
    assert (
        config["user.folia.plugins-manifest-url"]
        == "https://mgmt.example:8443/api/v1/worlds/world-overworld/plugins-manifest"
    )


def test_node_config_sets_datapacks_manifest_url_even_without_datapacks():
    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1)
    settings = Settings(public_url="https://mgmt.example:8443")
    config = _node_config(_session(), world, settings)
    assert (
        config["user.folia.datapacks-manifest-url"] == "https://mgmt.example:8443/api/v1/worlds/w/datapacks-manifest"
    )


def test_node_config_omits_datapacks_manifest_url_without_public_url():
    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8, datapacks=["Matcha"])
    settings = Settings(public_url=None)
    config = _node_config(_session(), world, settings)
    assert "user.folia.datapacks-manifest-url" not in config


def test_node_config_sets_datapacks_manifest_url_when_datapacks_and_public_url_present():
    world = World(name="world-overworld", type=WorldType.overworld, cpu_cores=4, memory_gb=8, datapacks=["Matcha"])
    settings = Settings(public_url="https://mgmt.example:8443/")
    config = _node_config(_session(), world, settings)
    assert (
        config["user.folia.datapacks-manifest-url"]
        == "https://mgmt.example:8443/api/v1/worlds/world-overworld/datapacks-manifest"
    )


def test_node_config_sets_server_properties_manifest_url_when_public_url_present():
    world = World(name="world-overworld", type=WorldType.overworld, cpu_cores=4, memory_gb=8)
    settings = Settings(public_url="https://mgmt.example:8443/")
    config = _node_config(_session(), world, settings)
    assert (
        config["user.folia.server-properties-manifest-url"]
        == "https://mgmt.example:8443/api/v1/worlds/world-overworld/server-properties-manifest"
    )


def test_node_config_omits_server_properties_manifest_url_without_public_url():
    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8)
    settings = Settings(public_url=None)
    config = _node_config(_session(), world, settings)
    assert "user.folia.server-properties-manifest-url" not in config


class _PingLXDClient:
    def __init__(self, unreachable: set[str] = frozenset()):
        self.unreachable = set(unreachable)
        self.pinged: list[str] = []

    def ping_host(self, host) -> bool:
        self.pinged.append(host.name)
        return host.name not in self.unreachable


def test_check_host_health_marks_unreachable_online_host_offline():
    session = _session()
    session.add(Host(name="host3", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.commit()

    check_host_health(session, _PingLXDClient(unreachable={"host3"}))

    host = session.exec(select(Host).where(Host.name == "host3")).first()
    assert host.status == HostStatus.offline


def test_check_host_health_recovers_offline_host_that_answers_again():
    session = _session()
    session.add(Host(name="host3", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.offline))
    session.commit()

    check_host_health(session, _PingLXDClient())  # reachable by default

    host = session.exec(select(Host).where(Host.name == "host3")).first()
    assert host.status == HostStatus.online


def test_check_host_health_leaves_reachable_online_host_alone():
    session = _session()
    session.add(Host(name="host1", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.commit()

    check_host_health(session, _PingLXDClient())

    host = session.exec(select(Host).where(Host.name == "host1")).first()
    assert host.status == HostStatus.online


def test_check_host_health_never_touches_draining_or_cordoned_hosts():
    session = _session()
    session.add(Host(name="draining-host", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.draining))
    session.add(Host(name="cordoned-host", address="1.2.3.5:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.cordoned))
    session.commit()

    ping = _PingLXDClient(unreachable={"draining-host", "cordoned-host"})
    check_host_health(session, ping)

    assert ping.pinged == []  # never even probed — operator-managed states are untouched
    draining = session.exec(select(Host).where(Host.name == "draining-host")).first()
    cordoned = session.exec(select(Host).where(Host.name == "cordoned-host")).first()
    assert draining.status == HostStatus.draining
    assert cordoned.status == HostStatus.cordoned


def test_migrate_worlds_off_draining_hosts_picks_best_fit_target():
    session = _session()
    session.add(Host(name="going-away", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.draining))
    session.add(Host(name="roomy", address="1.2.3.5:8443", capacity_cpu_cores=16, capacity_memory_gb=32, status=HostStatus.online))
    session.add(Host(name="snug", address="1.2.3.6:8443", capacity_cpu_cores=4, capacity_memory_gb=8, status=HostStatus.online))
    session.add(
        World(
            name="world-a",
            type=WorldType.overworld,
            cpu_cores=2,
            memory_gb=4,
            phase=WorldPhase.running,
            host_name="going-away",
            container_name="world-a",
            address="10.0.0.1:25565",
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    migrate_worlds_off_draining_hosts(session, lxd)

    world = session.exec(select(World).where(World.name == "world-a")).first()
    assert world.host_name == "roomy"  # most free capacity remaining after placement
    assert world.sticky_host == "roomy"
    assert world.phase == WorldPhase.provisioning
    assert world.address is None
    assert lxd.migrated == [("going-away", "world-a", "roomy")]
    assert lxd.deleted == [("going-away", "world-a")]


def test_migrate_worlds_off_draining_hosts_moves_crashed_worlds_too():
    session = _session()
    session.add(Host(name="going-away", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.draining))
    session.add(Host(name="target", address="1.2.3.5:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-crashed",
            type=WorldType.overworld,
            cpu_cores=2,
            memory_gb=4,
            phase=WorldPhase.crashed,
            host_name="going-away",
            container_name="world-crashed",
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    migrate_worlds_off_draining_hosts(session, lxd)

    world = session.exec(select(World).where(World.name == "world-crashed")).first()
    assert world.host_name == "target"
    assert world.phase == WorldPhase.provisioning  # migrated, not restarted in place
    assert lxd.migrated == [("going-away", "world-crashed", "target")]


def test_migrate_worlds_off_draining_hosts_leaves_world_in_place_without_capacity():
    session = _session()
    session.add(Host(name="going-away", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.draining))
    session.add(Host(name="full", address="1.2.3.5:8443", capacity_cpu_cores=1, capacity_memory_gb=1, status=HostStatus.online))
    session.add(
        World(
            name="world-a",
            type=WorldType.overworld,
            cpu_cores=4,
            memory_gb=8,
            phase=WorldPhase.running,
            host_name="going-away",
            container_name="world-a",
            address="10.0.0.1:25565",
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    migrate_worlds_off_draining_hosts(session, lxd)

    world = session.exec(select(World).where(World.name == "world-a")).first()
    assert world.host_name == "going-away"  # stays put, next tick retries
    assert world.phase == WorldPhase.running
    assert lxd.migrated == []


def test_migrate_worlds_off_draining_hosts_retries_on_migration_failure():
    session = _session()
    session.add(Host(name="going-away", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.draining))
    session.add(Host(name="target", address="1.2.3.5:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a",
            type=WorldType.overworld,
            cpu_cores=2,
            memory_gb=4,
            phase=WorldPhase.running,
            host_name="going-away",
            container_name="world-a",
            address="10.0.0.1:25565",
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    lxd.fail_migrate_for.add("world-a")
    migrate_worlds_off_draining_hosts(session, lxd)  # must not raise

    world = session.exec(select(World).where(World.name == "world-a")).first()
    assert world.host_name == "going-away"
    assert world.phase == WorldPhase.running


def test_migrate_worlds_off_draining_hosts_ignores_worlds_on_online_hosts():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a",
            type=WorldType.overworld,
            cpu_cores=2,
            memory_gb=4,
            phase=WorldPhase.running,
            host_name="node-a",
            container_name="world-a",
            address="10.0.0.1:25565",
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    migrate_worlds_off_draining_hosts(session, lxd)

    assert lxd.migrated == []
    world = session.exec(select(World).where(World.name == "world-a")).first()
    assert world.host_name == "node-a"


def test_sync_plugin_config_files_pushes_to_running_worlds_only():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-running", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-running",
            plugins=["Chunky"],
        )
    )
    session.add(
        World(
            name="world-pending", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.pending, plugins=["Chunky"],
        )
    )
    session.add(WorldPluginConfigFile(world_name="world-running", plugin_id="Chunky", path="config.yml", content=b"x", updated_by="op"))
    session.add(WorldPluginConfigFile(world_name="world-pending", plugin_id="Chunky", path="config.yml", content=b"x", updated_by="op"))
    session.commit()

    lxd = _RecordingLXDClient()
    sync_plugin_config_files(session, lxd)

    assert list(lxd.pushed.keys()) == [
        ("world-running", "/var/snap/folia-nexa-node/common/world/plugins/Chunky/config.yml")
    ]


def test_sync_plugin_config_files_tolerates_push_failure_and_continues():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            plugins=["Chunky"],
        )
    )
    session.add(WorldPluginConfigFile(world_name="world-a", plugin_id="Chunky", path="broken.yml", content=b"x", updated_by="op"))
    session.add(WorldPluginConfigFile(world_name="world-a", plugin_id="Chunky", path="ok.yml", content=b"y", updated_by="op"))
    session.commit()

    lxd = _RecordingLXDClient()
    lxd.fail_push_for.add("/var/snap/folia-nexa-node/common/world/plugins/Chunky/broken.yml")
    sync_plugin_config_files(session, lxd)  # must not raise

    assert ("world-a", "/var/snap/folia-nexa-node/common/world/plugins/Chunky/ok.yml") in lxd.pushed
    assert ("world-a", "/var/snap/folia-nexa-node/common/world/plugins/Chunky/broken.yml") not in lxd.pushed


def test_sync_plugin_config_files_stops_pushing_once_plugin_removed_from_world():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            plugins=[],  # Chunky already removed from the declared list
        )
    )
    session.add(WorldPluginConfigFile(world_name="world-a", plugin_id="Chunky", path="config.yml", content=b"x", updated_by="op"))
    session.commit()

    lxd = _RecordingLXDClient()
    sync_plugin_config_files(session, lxd)

    assert lxd.pushed == {}


def test_sync_plugin_config_files_skips_managed_plugin_rows():
    # Defense in depth: put_file (routers/plugin_config.py) already refuses
    # to create a WorldPluginConfigFile row for a managed plugin id, but a
    # row could still exist from before that check shipped (or from direct
    # DB manipulation). This reconcile loop must not push it — doing so
    # would silently and permanently clobber sync_luckperms_configs' own
    # config, since sync_plugin_config_files runs after it in reconcile().
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            plugins=["LuckPerms"],
        )
    )
    session.add(WorldPluginConfigFile(world_name="world-a", plugin_id="LuckPerms", path="config.yml", content=b"malicious-override", updated_by="op"))
    session.commit()

    lxd = _RecordingLXDClient()
    sync_plugin_config_files(session, lxd)

    assert lxd.pushed == {}


def test_run_scheduled_backups_snapshots_running_backups_enabled_world():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    run_scheduled_backups(session, lxd)

    assert len(lxd.snapshots) == 1
    host_name, container_name, snapshot_name = lxd.snapshots[0]
    assert (host_name, container_name) == ("node-a", "world-a")
    assert snapshot_name.startswith("auto-")

    rows = session.exec(select(WorldBackup).where(WorldBackup.world_name == "world-a")).all()
    assert len(rows) == 1
    assert rows[0].snapshot_name == snapshot_name
    assert rows[0].kind == "scheduled"


def test_run_scheduled_backups_skips_when_disabled():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=False,
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    run_scheduled_backups(session, lxd)

    assert lxd.snapshots == []
    assert session.exec(select(WorldBackup)).all() == []


def test_run_scheduled_backups_skips_non_running_worlds():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.stopped, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    run_scheduled_backups(session, lxd)

    assert lxd.snapshots == []


def test_run_scheduled_backups_skips_worlds_backed_up_within_the_last_hour():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.add(
        WorldBackup(
            world_name="world-a", snapshot_name="auto-recent", kind="scheduled",
            created_at=utcnow() - (BACKUP_INTERVAL - timedelta(minutes=5)),
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    run_scheduled_backups(session, lxd)

    assert lxd.snapshots == []
    assert len(session.exec(select(WorldBackup)).all()) == 1  # no new row added


def test_run_scheduled_backups_takes_a_new_backup_once_the_interval_has_passed():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.add(
        WorldBackup(
            world_name="world-a", snapshot_name="auto-old", kind="scheduled",
            created_at=utcnow() - (BACKUP_INTERVAL + timedelta(minutes=5)),
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    run_scheduled_backups(session, lxd)

    assert len(lxd.snapshots) == 1
    assert len(session.exec(select(WorldBackup)).all()) == 2


def test_run_scheduled_backups_tolerates_snapshot_failure_and_continues():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-broken", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-broken",
            backups_enabled=True,
        )
    )
    session.add(
        World(
            name="world-ok", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-ok",
            backups_enabled=True,
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    lxd.fail_snapshot_for.add("world-broken")
    run_scheduled_backups(session, lxd)  # must not raise

    names_backed_up = {row.world_name for row in session.exec(select(WorldBackup)).all()}
    assert names_backed_up == {"world-ok"}

    broken = session.exec(select(World).where(World.name == "world-broken")).one()
    assert broken.last_backup_error == "simulated snapshot failure for world-broken"
    assert broken.last_backup_attempt_at is not None

    ok = session.exec(select(World).where(World.name == "world-ok")).one()
    assert ok.last_backup_error is None
    assert ok.last_backup_attempt_at is not None


def test_run_scheduled_backups_clears_a_previously_recorded_error_on_success():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True, last_backup_error="stale failure from a previous tick",
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    run_scheduled_backups(session, lxd)

    world = session.exec(select(World).where(World.name == "world-a")).one()
    assert world.last_backup_error is None


def test_run_scheduled_backups_does_not_reset_a_backup_disabled_mid_flight():
    """Regression test: an operator's PUT .../backups-config can disable
    backups (and clear a stale error) in a separate session while this
    world's snapshot_container call is still in flight — since it was
    already loaded here with backups_enabled=True. A failure that
    surfaces after that point must not stamp last_backup_error back onto
    a world whose backups were just turned off, since a disabled world
    is never revisited by this loop to clear it again."""
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.commit()

    other_session = Session(session.bind)

    class _DisablingLXDClient(_RecordingLXDClient):
        def snapshot_container(self, host, name, snapshot_name):
            # Simulates a concurrent operator PUT .../backups-config
            # {enabled: false} committing, in its own session, while this
            # call is in flight.
            world = other_session.exec(select(World).where(World.name == name)).one()
            world.backups_enabled = False
            world.last_backup_error = None
            world.last_backup_attempt_at = None
            other_session.add(world)
            other_session.commit()
            return super().snapshot_container(host, name, snapshot_name)

    lxd = _DisablingLXDClient()
    lxd.fail_snapshot_for.add("world-a")

    run_scheduled_backups(session, lxd)

    world = session.exec(select(World).where(World.name == "world-a")).one()
    assert world.backups_enabled is False
    assert world.last_backup_error is None
    assert world.last_backup_attempt_at is None


def test_run_scheduled_backups_still_records_a_snapshot_that_succeeds_despite_being_disabled_mid_flight():
    """Regression test: the snapshot_container call itself can *succeed*
    in the same race the test above covers for failure. A snapshot that
    succeeds is really sitting on the host's storage pool regardless of
    whether backups_enabled flipped to False while the call was in
    flight — it must still get a WorldBackup row, or it becomes an
    orphaned, untracked, never-pruned snapshot that the dashboard can
    never show or restore from."""
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.commit()

    other_session = Session(session.bind)

    class _DisablingLXDClient(_RecordingLXDClient):
        def snapshot_container(self, host, name, snapshot_name):
            # Simulates a concurrent operator PUT .../backups-config
            # {enabled: false} committing, in its own session, while this
            # call is in flight -- but unlike the test above, the
            # snapshot itself still succeeds.
            world = other_session.exec(select(World).where(World.name == name)).one()
            world.backups_enabled = False
            other_session.add(world)
            other_session.commit()
            return super().snapshot_container(host, name, snapshot_name)

    lxd = _DisablingLXDClient()

    run_scheduled_backups(session, lxd)

    world = session.exec(select(World).where(World.name == "world-a")).one()
    assert world.backups_enabled is False
    # The world itself is untouched (disabled mid-flight, never revisited)...
    assert world.last_backup_attempt_at is None
    assert world.last_backup_error is None
    # ...but the snapshot that actually landed on the host is still tracked.
    backups = session.exec(select(WorldBackup).where(WorldBackup.world_name == "world-a")).all()
    assert len(backups) == 1
    assert backups[0].kind == "scheduled"


def test_run_scheduled_backups_tolerates_world_hard_deleted_mid_flight():
    """Regression test: a world can be hard-deleted (DELETE /worlds/{name}
    -> teardown_world, once its container is actually gone) by a
    concurrent request while this world's own snapshot_container call is
    still in flight. backups_still_enabled() must not blindly
    session.refresh() a row that's no longer there — that raises
    ObjectDeletedError, which (uncaught) would abort the whole
    reconcile-tick session and roll back every other world's
    already-committed backup progress from the same pass."""
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.commit()

    other_session = Session(session.bind)

    class _DeletingLXDClient(_RecordingLXDClient):
        def snapshot_container(self, host, name, snapshot_name):
            # Simulates a concurrent DELETE /worlds/world-a -> teardown_world
            # hard-deleting the row while this call is in flight, and the
            # snapshot attempt itself failing.
            world = other_session.exec(select(World).where(World.name == name)).one()
            other_session.delete(world)
            other_session.commit()
            raise LXDError(f"simulated snapshot failure for {name}")

    lxd = _DeletingLXDClient()

    run_scheduled_backups(session, lxd)  # must not raise

    assert session.exec(select(World).where(World.name == "world-a")).first() is None


def test_run_scheduled_backups_commits_per_world_so_a_later_worlds_call_cant_clobber_an_earlier_disable():
    """Regression test: the loop used to batch every world's
    last_backup_attempt_at/last_backup_error write into a single commit
    at the very end. That left a wide window — the time spent on every
    *other* world's own (potentially slow) LXD call — during which an
    operator's PUT .../backups-config disable-and-clear for an
    already-processed world could land, commit, and then get silently
    overwritten once the batched commit finally flushed that world's
    now-stale in-memory attempt_at/error back over it. Committing
    per-world closes that window."""
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.add(
        World(
            name="world-b", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-b",
            backups_enabled=True,
        )
    )
    session.commit()

    other_session = Session(session.bind)

    class _RaceyLXDClient(_RecordingLXDClient):
        def snapshot_container(self, host, name, snapshot_name):
            if name == "world-b":
                # Simulates an operator's PUT .../backups-config disabling
                # and clearing world-a's error, landing while this loop is
                # still busy processing the later world-b.
                world_a = other_session.exec(select(World).where(World.name == "world-a")).one()
                world_a.backups_enabled = False
                world_a.last_backup_error = None
                world_a.last_backup_attempt_at = None
                other_session.add(world_a)
                other_session.commit()
            return super().snapshot_container(host, name, snapshot_name)

    lxd = _RaceyLXDClient()
    lxd.fail_snapshot_for.add("world-a")

    run_scheduled_backups(session, lxd)

    world_a = session.exec(select(World).where(World.name == "world-a")).one()
    assert world_a.backups_enabled is False
    assert world_a.last_backup_error is None
    assert world_a.last_backup_attempt_at is None


def test_prune_expired_backups_deletes_snapshot_and_row_older_than_a_week():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
        )
    )
    session.add(
        WorldBackup(
            world_name="world-a", snapshot_name="auto-ancient", kind="scheduled",
            created_at=utcnow() - (BACKUP_RETENTION + timedelta(hours=1)),
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    prune_expired_backups(session, lxd)

    assert ("node-a", "world-a", "auto-ancient") in lxd.deleted_snapshots
    assert session.exec(select(WorldBackup)).all() == []


def test_prune_expired_backups_keeps_backups_within_retention():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
        )
    )
    session.add(
        WorldBackup(
            world_name="world-a", snapshot_name="auto-recent", kind="scheduled",
            created_at=utcnow() - timedelta(days=1),
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    prune_expired_backups(session, lxd)

    assert lxd.deleted_snapshots == []
    assert len(session.exec(select(WorldBackup)).all()) == 1


def test_prune_expired_backups_retries_on_snapshot_delete_failure():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
        )
    )
    session.add(
        WorldBackup(
            world_name="world-a", snapshot_name="auto-ancient", kind="scheduled",
            created_at=utcnow() - (BACKUP_RETENTION + timedelta(hours=1)),
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    lxd.fail_delete_snapshot_for.add("auto-ancient")
    prune_expired_backups(session, lxd)  # must not raise

    assert lxd.deleted_snapshots == []
    # row kept so the next reconcile tick retries the snapshot deletion
    assert len(session.exec(select(WorldBackup)).all()) == 1


def test_prune_expired_backups_drops_row_when_world_no_longer_exists():
    # A world can be deleted entirely while its old backup rows are still
    # pending expiry — nothing left to reach on LXD's side, so just drop
    # the now-orphaned row rather than getting stuck forever.
    session = _session()
    session.add(
        WorldBackup(
            world_name="world-gone", snapshot_name="auto-ancient", kind="scheduled",
            created_at=utcnow() - (BACKUP_RETENTION + timedelta(hours=1)),
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    prune_expired_backups(session, lxd)  # must not raise

    assert session.exec(select(WorldBackup)).all() == []


def test_teardown_world_deletes_its_backup_rows():
    # World.name is reusable once its row is gone (teardown_world's own
    # docstring) — WorldBackup rows are keyed on that bare name string with
    # no FK, so a new world created with the same name must not inherit an
    # old, unrelated world's backup history.
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    world = World(
        name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        phase=WorldPhase.draining, host_name="node-a", container_name="world-a",
    )
    session.add(world)
    session.add(WorldBackup(world_name="world-a", snapshot_name="auto-1", kind="scheduled"))
    session.add(WorldBackup(world_name="world-a", snapshot_name="auto-2", kind="scheduled"))
    session.commit()

    lxd = _RecordingLXDClient()
    teardown_world(session, lxd, world)

    assert session.get(World, world.id) is None
    assert session.exec(select(WorldBackup).where(WorldBackup.world_name == "world-a")).all() == []


def test_teardown_world_leaves_backup_rows_when_container_delete_fails():
    # Teardown itself retries next tick on failure (the pre-existing
    # behavior) — the backup rows must survive that retry too, not be
    # dropped before the world row they belong to is actually gone.
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    world = World(
        name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        phase=WorldPhase.draining, host_name="node-a", container_name="world-a",
    )
    session.add(world)
    session.add(WorldBackup(world_name="world-a", snapshot_name="auto-1", kind="scheduled"))
    session.commit()

    class _FailingLXDClient(_RecordingLXDClient):
        def delete_container(self, host, name, *, stop_first=True):
            raise LXDError("simulated delete failure")

    teardown_world(session, _FailingLXDClient(), world)

    assert session.get(World, world.id) is not None
    assert len(session.exec(select(WorldBackup).where(WorldBackup.world_name == "world-a")).all()) == 1


def test_run_scheduled_backups_skips_worlds_on_an_offline_host():
    # A host outage must not turn into a fresh LXD call (and a fresh
    # exception log) for every affected world on every 15s reconcile tick —
    # check_host_health (which runs earlier in the same reconcile pass)
    # already marks the host offline, so this waits for it to come back
    # rather than hammering it.
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.offline))
    session.add(
        World(
            name="world-a", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-a",
            backups_enabled=True,
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    run_scheduled_backups(session, lxd)  # must not raise

    assert lxd.snapshots == []
    assert session.exec(select(WorldBackup)).all() == []
