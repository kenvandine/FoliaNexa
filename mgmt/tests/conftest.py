from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from folia_mgmt.auth import hash_password
from folia_mgmt.db import get_engine, init_db
from folia_mgmt.deps import get_lxd_client
from folia_mgmt.lxd_client import LXDError
from folia_mgmt.models import User, UserRole


class FakeLXDClient:
    """Stands in for LXDClient in tests — no network/TLS involved. Records
    calls so tests can assert on what the scheduler tried to do."""

    def __init__(self):
        self.launched: list[tuple[str, str]] = []  # (host_name, container_name)
        self.deleted: list[tuple[str, str]] = []
        self.snapshots: list[tuple[str, str, str]] = []
        self.restores: list[tuple[str, str, str]] = []
        self._next_ip = 10
        self.fail_launch_for: set[str] = set()

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

    def snapshot_container(self, host, name, snapshot_name):
        self.snapshots.append((host.name, name, snapshot_name))

    def restore_snapshot(self, host, name, snapshot_name):
        self.restores.append((host.name, name, snapshot_name))


@pytest.fixture
def fake_lxd():
    return FakeLXDClient()


@pytest.fixture
def app(tmp_path, monkeypatch, fake_lxd):
    monkeypatch.setenv("FOLIA_MGMT_STATE_DIR", str(tmp_path / "state"))
    # Import after the env var is set so any module-level Settings() reads
    # (there shouldn't be any, but this keeps the fixture order foolproof).
    from folia_mgmt.main import create_app

    application = create_app()
    application.dependency_overrides[get_lxd_client] = lambda: fake_lxd

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
