from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

from folia_mgmt.config import Settings
from folia_mgmt.luckperms import LUCKPERMS_CONFIG_PATH, apply_luckperms_config, render_luckperms_config
from folia_mgmt.models import Host, HostStatus, World, WorldPhase, WorldType
from folia_mgmt.scheduler import sync_luckperms_configs


def _configured_settings(**overrides) -> Settings:
    defaults = dict(
        luckperms_mysql_host="10.0.5.10",
        luckperms_mysql_port=3306,
        luckperms_mysql_database="luckperms",
        luckperms_mysql_user="luckperms",
        luckperms_mysql_password="hunter2",
        luckperms_table_prefix="luckperms_",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


class _RecordingLXDClient:
    def __init__(self, fail: bool = False):
        self.pushed: dict[tuple[str, str], bytes] = {}
        self._fail = fail

    def push_file(self, host, name, path, content, *, mode="0644"):
        if self._fail:
            from folia_mgmt.lxd_client import LXDError

            raise LXDError("plugin folder doesn't exist yet")
        self.pushed[(name, path)] = content


def test_render_includes_connection_details():
    content = render_luckperms_config("world-overworld", _configured_settings()).decode()
    assert "storage-method: mysql" in content
    assert "address: 10.0.5.10:3306" in content
    assert "database: luckperms" in content
    assert "username: luckperms" in content
    assert "password: hunter2" in content
    assert "table_prefix: 'luckperms_'" in content
    assert "server: 'world-overworld'" in content


def test_apply_skips_when_not_configured():
    lxd = _RecordingLXDClient()
    host = Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16)
    world = World(
        name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        container_name="world-overworld", plugins=["LuckPerms"],
    )
    apply_luckperms_config(lxd, host, world, Settings())  # no luckperms_mysql_host set
    assert lxd.pushed == {}


def test_apply_skips_world_without_luckperms_plugin():
    lxd = _RecordingLXDClient()
    host = Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16)
    world = World(
        name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        container_name="world-overworld", plugins=["HuskClaims"],
    )
    apply_luckperms_config(lxd, host, world, _configured_settings())
    assert lxd.pushed == {}


def test_apply_pushes_config_when_configured_and_plugin_present():
    lxd = _RecordingLXDClient()
    host = Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16)
    world = World(
        name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        container_name="world-overworld", plugins=["LuckPerms", "HuskClaims"],
    )
    apply_luckperms_config(lxd, host, world, _configured_settings())

    key = ("world-overworld", LUCKPERMS_CONFIG_PATH)
    assert key in lxd.pushed
    assert b"world-overworld" in lxd.pushed[key]


def test_apply_push_failure_does_not_raise():
    lxd = _RecordingLXDClient(fail=True)
    host = Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16)
    world = World(
        name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        container_name="world-overworld", plugins=["LuckPerms"],
    )
    apply_luckperms_config(lxd, host, world, _configured_settings())  # must not raise


def test_sync_luckperms_configs_only_touches_running_worlds_with_plugin():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-overworld",
            plugins=["LuckPerms"],
        )
    )
    session.add(
        World(
            name="world-nether", type=WorldType.nether, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-nether",
            plugins=["Spark"],  # no LuckPerms
        )
    )
    session.add(
        World(
            name="world-pending", type=WorldType.lobby, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.pending, plugins=["LuckPerms"],  # not running yet
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    sync_luckperms_configs(session, lxd, _configured_settings())

    assert list(lxd.pushed.keys()) == [("world-overworld", LUCKPERMS_CONFIG_PATH)]


def test_sync_luckperms_configs_noop_when_not_configured():
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-overworld",
            plugins=["LuckPerms"],
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    sync_luckperms_configs(session, lxd, Settings())  # not configured
    assert lxd.pushed == {}
