from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

from folia_mgmt.config import Settings
from folia_mgmt.folianexa_stats import (
    STATS_CONFIG_PATH,
    STATS_SERVICE_ACCOUNT_USERNAME,
    apply_stats_config,
    get_or_create_stats_api_token,
    render_stats_config,
)
from folia_mgmt.lxd_client import LXDError
from folia_mgmt.models import ApiToken, Host, HostStatus, User, UserRole, World, WorldPhase, WorldType
from folia_mgmt.scheduler import sync_stats_configs


def _configured_settings(**overrides) -> Settings:
    defaults = dict(public_url="https://mgmt.example:8443")
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
            raise LXDError("plugin folder doesn't exist yet")
        self.pushed[(name, path)] = content


def test_render_includes_url_and_token():
    content = render_stats_config("https://mgmt.example:8443", "s3cr3t-token").decode()
    assert 'mgmt-base-url: "https://mgmt.example:8443"' in content
    assert 'mgmt-api-token: "s3cr3t-token"' in content
    assert "report-interval-seconds: 60" in content


def test_get_or_create_stats_api_token_creates_operator_service_account(tmp_path):
    session = _session()
    settings = _configured_settings(state_dir=tmp_path)

    token = get_or_create_stats_api_token(session, settings)
    assert token

    user = session.exec(select(User).where(User.username == STATS_SERVICE_ACCOUNT_USERNAME)).first()
    assert user is not None
    assert user.role == UserRole.operator

    # a real ApiToken row exists and hashes to the same token returned
    from folia_mgmt.auth import hash_token

    api_token = session.exec(select(ApiToken).where(ApiToken.user_id == user.id)).first()
    assert api_token is not None
    assert api_token.token_hash == hash_token(token)


def test_get_or_create_stats_api_token_persists_and_reuses(tmp_path):
    session = _session()
    settings = _configured_settings(state_dir=tmp_path)

    first = get_or_create_stats_api_token(session, settings)
    second = get_or_create_stats_api_token(session, settings)
    assert first == second

    # only one service account / token ever created
    users = session.exec(select(User).where(User.username == STATS_SERVICE_ACCOUNT_USERNAME)).all()
    assert len(users) == 1
    tokens = session.exec(select(ApiToken).where(ApiToken.user_id == users[0].id)).all()
    assert len(tokens) == 1


def test_apply_skips_world_without_stats_plugin(tmp_path):
    session = _session()
    lxd = _RecordingLXDClient()
    host = Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16)
    world = World(
        name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        container_name="world-overworld", plugins=["HuskClaims"],
    )
    apply_stats_config(session, lxd, host, world, _configured_settings(state_dir=tmp_path))
    assert lxd.pushed == {}


def test_apply_skips_without_public_url(tmp_path):
    session = _session()
    lxd = _RecordingLXDClient()
    host = Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16)
    world = World(
        name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        container_name="world-overworld", plugins=["FoliaNexaStats"],
    )
    apply_stats_config(session, lxd, host, world, Settings(public_url=None, state_dir=tmp_path))
    assert lxd.pushed == {}


def test_apply_pushes_config_with_real_token(tmp_path):
    session = _session()
    lxd = _RecordingLXDClient()
    host = Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16)
    world = World(
        name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        container_name="world-overworld", plugins=["FoliaNexaStats"],
    )
    settings = _configured_settings(state_dir=tmp_path)
    apply_stats_config(session, lxd, host, world, settings)

    key = ("world-overworld", STATS_CONFIG_PATH)
    assert key in lxd.pushed
    content = lxd.pushed[key].decode()
    assert 'mgmt-base-url: "https://mgmt.example:8443"' in content
    assert "mgmt-api-token:" in content
    assert 'mgmt-api-token: ""' not in content


def test_apply_push_failure_does_not_raise(tmp_path):
    session = _session()
    lxd = _RecordingLXDClient(fail=True)
    host = Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16)
    world = World(
        name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
        container_name="world-overworld", plugins=["FoliaNexaStats"],
    )
    apply_stats_config(session, lxd, host, world, _configured_settings(state_dir=tmp_path))  # must not raise


def test_sync_stats_configs_only_touches_running_worlds_with_plugin(tmp_path):
    session = _session()
    session.add(Host(name="node-a", address="1.2.3.4:8443", capacity_cpu_cores=8, capacity_memory_gb=16, status=HostStatus.online))
    session.add(
        World(
            name="world-overworld", type=WorldType.overworld, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-overworld",
            plugins=["FoliaNexaStats"],
        )
    )
    session.add(
        World(
            name="world-nether", type=WorldType.nether, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.running, host_name="node-a", container_name="world-nether",
            plugins=["Spark"],  # no FoliaNexaStats
        )
    )
    session.add(
        World(
            name="world-pending", type=WorldType.lobby, cpu_cores=1, memory_gb=1,
            phase=WorldPhase.pending, plugins=["FoliaNexaStats"],  # not running yet
        )
    )
    session.commit()

    lxd = _RecordingLXDClient()
    sync_stats_configs(session, lxd, _configured_settings(state_dir=tmp_path))

    assert list(lxd.pushed.keys()) == [("world-overworld", STATS_CONFIG_PATH)]
