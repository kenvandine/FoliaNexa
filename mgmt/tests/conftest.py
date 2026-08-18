from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from folia_mgmt.auth import hash_password
from folia_mgmt.db import get_engine, init_db
from folia_mgmt.deps import get_health_check, get_lxd_client, get_uuid_resolver
from folia_mgmt.lxd_client import LXDError
from folia_mgmt.models import User, UserRole
from folia_mgmt.scheduler import reconcile


class FakeLXDClient:
    """Stands in for LXDClient in tests — no network/TLS involved. Records
    calls so tests can assert on what the scheduler tried to do."""

    def __init__(self):
        self.launched: list[tuple[str, str]] = []  # (host_name, container_name)
        self.deleted: list[tuple[str, str]] = []
        self.restarted: list[tuple[str, str]] = []
        self.stopped: list[tuple[str, str]] = []
        self.started: list[tuple[str, str]] = []
        self.snapshots: list[tuple[str, str, str]] = []
        self.restores: list[tuple[str, str, str]] = []
        self._next_ip = 10
        self.fail_launch_for: set[str] = set()
        self.pushed_files: dict[tuple[str, str, str], bytes] = {}  # (host_name, container, path) -> content
        self.migrations: list[tuple[str, str, str]] = []  # (source_host, container, target_host)
        self.fail_migrate_for: set[str] = set()
        self.updated_config: dict[tuple[str, str], dict] = {}  # (host_name, container) -> last config pushed
        self.unreachable: set[str] = set()  # host names that ping_host should report as down
        self.ping_calls: list[str] = []

    def ping_host(self, host) -> bool:
        self.ping_calls.append(host.name)
        return host.name not in self.unreachable

    def redeem_trust_token(self, address: str, project: str, trust_token: str):
        if trust_token == "bad-token":
            raise LXDError("token rejected by host")
        return f"fingerprint-of-{address}", f"-----BEGIN CERTIFICATE-----\nfake-{address}\n-----END CERTIFICATE-----"

    def launch_container(self, host, name, image_alias, *, cpu_cores, memory_gb, config=None, snapshot_schedule=None, snapshot_expiry=None, profiles=None):
        if name in self.fail_launch_for:
            raise LXDError(f"simulated launch failure for {name}")
        self.launched.append((host.name, name))
        return {"status": "Success"}

    def get_instance_state(self, host, name):
        self._next_ip += 1
        return {
            "network": {
                "eth0": {
                    "addresses": [
                        {"family": "inet", "address": f"10.0.1.{self._next_ip}", "scope": "global"}
                    ]
                }
            }
        }

    def delete_container(self, host, name, *, stop_first=True):
        self.deleted.append((host.name, name))

    def restart_container(self, host, name):
        self.restarted.append((host.name, name))

    def stop_container(self, host, name):
        self.stopped.append((host.name, name))

    def start_container(self, host, name):
        self.started.append((host.name, name))

    def update_config(self, host, name, config):
        self.updated_config[(host.name, name)] = config

    def snapshot_container(self, host, name, snapshot_name):
        self.snapshots.append((host.name, name, snapshot_name))

    def restore_snapshot(self, host, name, snapshot_name):
        self.restores.append((host.name, name, snapshot_name))

    def push_file(self, host, name, path, content, *, mode="0644"):
        self.pushed_files[(host.name, name, path)] = content

    def migrate_container(self, source_host, source_name, target_host):
        if source_name in self.fail_migrate_for:
            raise LXDError(f"simulated migration failure for {source_name}")
        self.migrations.append((source_host.name, source_name, target_host.name))


@pytest.fixture
def fake_lxd():
    return FakeLXDClient()


class FakeHealthCheck:
    """Defaults every world to healthy — tests that want to exercise crash
    detection mark specific world names unhealthy via `.unhealthy.add(name)`."""

    def __init__(self):
        self.unhealthy: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, world, settings) -> bool:
        self.calls.append(world.name)
        return world.name not in self.unhealthy


@pytest.fixture
def fake_health_check():
    return FakeHealthCheck()


class FakeUuidResolver:
    """Deterministic name->UUID mapping — no real Mojang API calls in tests."""

    def __call__(self, username: str) -> str | None:
        if username == "unresolvable":
            return None
        return (username.lower() + "0" * 32)[:32]


@pytest.fixture
def fake_uuid_resolver():
    return FakeUuidResolver()


@pytest.fixture
def app(tmp_path, monkeypatch, fake_lxd, fake_health_check, fake_uuid_resolver):
    monkeypatch.setenv("FOLIA_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FOLIA_MGMT_PUBLIC_URL", "https://mgmt.example:8443")
    # Import after the env var is set so any module-level Settings() reads
    # (there shouldn't be any, but this keeps the fixture order foolproof).
    from folia_mgmt.main import create_app

    application = create_app()
    application.dependency_overrides[get_lxd_client] = lambda: fake_lxd
    application.dependency_overrides[get_health_check] = lambda: fake_health_check
    application.dependency_overrides[get_uuid_resolver] = lambda: fake_uuid_resolver

    settings_state_dir = tmp_path / "state"
    settings_state_dir.mkdir(parents=True, exist_ok=True)

    from folia_mgmt.config import Settings

    settings = Settings(state_dir=settings_state_dir)
    settings.certs_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings)

    application.state.test_settings = settings
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_session(app):
    engine = get_engine(app.state.test_settings)
    with Session(engine) as session:
        yield session


@pytest.fixture
def trigger_reconcile(app, fake_lxd, fake_health_check):
    """Runs one real reconcile() pass directly against the test's own
    engine/fake LXD client/fake health check — used by tests that need to
    force a pass through scheduler logic (health recovery, host draining,
    migration) without waiting on the periodic background loop. POST
    /worlds used to double as a way to trigger this (it ran a full
    reconcile()), but that's no longer true — world creation now only
    places the world being created, not the whole cluster — so tests that
    need a full pass call this directly instead."""

    def _trigger():
        engine = get_engine(app.state.test_settings)
        with Session(engine) as session:
            reconcile(session, fake_lxd, settings=app.state.test_settings, health_check=fake_health_check)

    return _trigger


@pytest.fixture
def admin_token(app, client):
    engine = get_engine(app.state.test_settings)
    with Session(engine) as session:
        session.add(User(username="admin", password_hash=hash_password("adminpass"), role=UserRole.admin))
        session.commit()
    resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "adminpass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


@pytest.fixture
def operator_token(app, client):
    engine = get_engine(app.state.test_settings)
    with Session(engine) as session:
        session.add(User(username="op", password_hash=hash_password("oppass"), role=UserRole.operator))
        session.commit()
    resp = client.post("/api/v1/auth/login", json={"username": "op", "password": "oppass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


@pytest.fixture
def viewer_token(app, client):
    engine = get_engine(app.state.test_settings)
    with Session(engine) as session:
        session.add(User(username="view", password_hash=hash_password("viewpass"), role=UserRole.viewer))
        session.commit()
    resp = client.post("/api/v1/auth/login", json={"username": "view", "password": "viewpass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]
