from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

from folia_mgmt.config import Settings
from folia_mgmt.models import Host, HostStatus, World, WorldPhase, WorldType
from folia_mgmt.scheduler import check_running_worlds, recover_crashed_worlds, select_host


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

    def restart_container(self, host, name):
        self.restarted.append(name)


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
