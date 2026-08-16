from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

from folia_mgmt.config import Settings
from folia_mgmt.models import AccessRequest, AccessRequestStatus, Host, HostStatus, World, WorldPhase, WorldType
from folia_mgmt.scheduler import (
    _node_config,
    check_running_worlds,
    recover_crashed_worlds,
    select_host,
    sync_whitelisted_worlds,
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

    def restart_container(self, host, name):
        self.restarted.append(name)

    def push_file(self, host, name, path, content, *, mode="0644"):
        self.pushed[(name, path)] = content


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
    config = _node_config(world, settings)
    assert config["user.folia.plugins-manifest-url"] == "https://mgmt.example:8443/api/v1/worlds/w/plugins-manifest"
    assert config["user.folia.jar-url"] == f"{settings.artifacts_base_url}/{world.engine}/{world.version}/{world.engine}.jar"


def test_node_config_omits_manifest_url_without_public_url():
    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8, plugins=["LuckPerms"])
    settings = Settings(public_url=None)
    config = _node_config(world, settings)
    assert "user.folia.plugins-manifest-url" not in config


def test_node_config_sets_manifest_url_when_plugins_and_public_url_present():
    world = World(name="world-overworld", type=WorldType.overworld, cpu_cores=4, memory_gb=8, plugins=["LuckPerms", "Spark"])
    settings = Settings(public_url="https://mgmt.example:8443/")
    config = _node_config(world, settings)
    assert (
        config["user.folia.plugins-manifest-url"]
        == "https://mgmt.example:8443/api/v1/worlds/world-overworld/plugins-manifest"
    )


def test_node_config_sets_datapacks_manifest_url_even_without_datapacks():
    world = World(name="w", type=WorldType.lobby, cpu_cores=1, memory_gb=1)
    settings = Settings(public_url="https://mgmt.example:8443")
    config = _node_config(world, settings)
    assert (
        config["user.folia.datapacks-manifest-url"] == "https://mgmt.example:8443/api/v1/worlds/w/datapacks-manifest"
    )


def test_node_config_omits_datapacks_manifest_url_without_public_url():
    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8, datapacks=["Matcha"])
    settings = Settings(public_url=None)
    config = _node_config(world, settings)
    assert "user.folia.datapacks-manifest-url" not in config


def test_node_config_sets_datapacks_manifest_url_when_datapacks_and_public_url_present():
    world = World(name="world-overworld", type=WorldType.overworld, cpu_cores=4, memory_gb=8, datapacks=["Matcha"])
    settings = Settings(public_url="https://mgmt.example:8443/")
    config = _node_config(world, settings)
    assert (
        config["user.folia.datapacks-manifest-url"]
        == "https://mgmt.example:8443/api/v1/worlds/world-overworld/datapacks-manifest"
    )


def test_node_config_sets_server_properties_manifest_url_when_public_url_present():
    world = World(name="world-overworld", type=WorldType.overworld, cpu_cores=4, memory_gb=8)
    settings = Settings(public_url="https://mgmt.example:8443/")
    config = _node_config(world, settings)
    assert (
        config["user.folia.server-properties-manifest-url"]
        == "https://mgmt.example:8443/api/v1/worlds/world-overworld/server-properties-manifest"
    )


def test_node_config_omits_server_properties_manifest_url_without_public_url():
    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8)
    settings = Settings(public_url=None)
    config = _node_config(world, settings)
    assert "user.folia.server-properties-manifest-url" not in config
