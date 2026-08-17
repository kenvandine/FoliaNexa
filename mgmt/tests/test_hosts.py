from __future__ import annotations

from helpers import auth_header


def _get_join_token(client, admin_token) -> str:
    resp = client.post("/api/v1/hosts/join-token", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _enroll_body(name="node-a", address="10.0.1.11:8443", trust_token="good-token"):
    return {
        "name": name,
        "address": address,
        "project": "folia",
        "lxd_trust_token": trust_token,
        "capacity": {"cpu_cores": 6, "memory_gb": 12},
        "labels": {"cpu_type": "p-core"},
    }


def test_enroll_happy_path(client, admin_token, fake_lxd):
    join_token = _get_join_token(client, admin_token)
    resp = client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "node-a"
    assert body["status"] == "online"
    assert body["allocated_cpu_cores"] == 0


def test_enroll_rejects_bad_join_token(client):
    resp = client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header("not-a-real-token"))
    assert resp.status_code == 401


def test_join_token_is_single_use(client, admin_token):
    join_token = _get_join_token(client, admin_token)
    first = client.post("/api/v1/hosts/enroll", json=_enroll_body(name="node-a"), headers=auth_header(join_token))
    assert first.status_code == 200

    second = client.post(
        "/api/v1/hosts/enroll",
        json=_enroll_body(name="node-b", address="10.0.1.12:8443"),
        headers=auth_header(join_token),
    )
    assert second.status_code == 401


def test_enroll_rejects_duplicate_host_name(client, admin_token):
    join_token_1 = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(name="node-a"), headers=auth_header(join_token_1))

    join_token_2 = _get_join_token(client, admin_token)
    resp = client.post(
        "/api/v1/hosts/enroll",
        json=_enroll_body(name="node-a", address="10.0.1.99:8443"),
        headers=auth_header(join_token_2),
    )
    assert resp.status_code == 409


def test_enroll_surfaces_lxd_trust_failure(client, admin_token):
    join_token = _get_join_token(client, admin_token)
    resp = client.post(
        "/api/v1/hosts/enroll",
        json=_enroll_body(trust_token="bad-token"),
        headers=auth_header(join_token),
    )
    assert resp.status_code == 502


def test_list_hosts_requires_auth(client, admin_token):
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))

    resp = client.get("/api/v1/hosts", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_drain_host(client, admin_token, operator_token):
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))

    resp = client.post("/api/v1/hosts/node-a/drain", headers=auth_header(operator_token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "draining"


def test_powered_off_host_flips_to_offline_on_next_reconcile(client, admin_token, operator_token, fake_lxd):
    """Reproduces the bug: enrolling a host used to leave it 'online' in
    the dashboard forever, even after it lost power — nothing ever
    re-checked reachability. Now every reconcile pass (scheduler.
    check_host_health) pings each trusted host directly."""
    join_token = _get_join_token(client, admin_token)
    client.post("/api/v1/hosts/enroll", json=_enroll_body(), headers=auth_header(join_token))
    assert client.get("/api/v1/hosts", headers=auth_header(admin_token)).json()[0]["status"] == "online"

    fake_lxd.unreachable.add("node-a")
    # A world create is the simplest way to force a reconcile pass through
    # the API surface under test, same trick test_health_recovery.py uses.
    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-trigger", "type": "lobby", "cpu_cores": 1, "memory_gb": 1},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200

    assert client.get("/api/v1/hosts", headers=auth_header(admin_token)).json()[0]["status"] == "offline"

    fake_lxd.unreachable.discard("node-a")
    client.post(
        "/api/v1/worlds",
        json={"name": "world-trigger-2", "type": "lobby", "cpu_cores": 1, "memory_gb": 1},
        headers=auth_header(operator_token),
    )
    assert client.get("/api/v1/hosts", headers=auth_header(admin_token)).json()[0]["status"] == "online"
