from __future__ import annotations

from helpers import auth_header


def _enroll_host(client, admin_token, **overrides):
    join = client.post("/api/v1/hosts/join-token", headers=auth_header(admin_token)).json()["token"]
    body = {
        "name": "node-a",
        "address": "10.0.1.11:8443",
        "project": "folia",
        "lxd_trust_token": "good-token",
        "capacity": {"cpu_cores": 6, "memory_gb": 12},
        "labels": {},
    }
    body.update(overrides)
    resp = client.post("/api/v1/hosts/enroll", json=body, headers=auth_header(join))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_running_world(client, operator_token, name="world-overworld", **overrides):
    body = {"name": name, "type": "overworld", "cpu_cores": 4, "memory_gb": 8}
    body.update(overrides)
    resp = client.post("/api/v1/worlds", json=body, headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["phase"] == "running"
    return resp.json()


def test_migrate_moves_world_to_target_host(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token, name="node-a", address="10.0.1.11:8443")
    _enroll_host(client, admin_token, name="node-b", address="10.0.1.12:8443")
    _create_running_world(client, operator_token, "world-overworld")

    resp = client.post(
        "/api/v1/worlds/world-overworld/migrate",
        params={"target_host": "node-b"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["host_name"] == "node-b"
    # cut over to provisioning, not left claiming "running" against a
    # possibly-stale address — the next reconcile pass re-polls it
    assert body["phase"] in ("provisioning", "running")

    assert ("node-a", "world-overworld", "node-b") in fake_lxd.migrations
    assert ("node-a", "world-overworld") in fake_lxd.deleted


def test_migrate_rejects_unknown_target_host(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    _create_running_world(client, operator_token)

    resp = client.post(
        "/api/v1/worlds/world-overworld/migrate",
        params={"target_host": "does-not-exist"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 404


def test_migrate_rejects_same_host(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    _create_running_world(client, operator_token)

    resp = client.post(
        "/api/v1/worlds/world-overworld/migrate",
        params={"target_host": "node-a"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 400


def test_migrate_rejects_offline_target(client, admin_token, operator_token):
    _enroll_host(client, admin_token, name="node-a", address="10.0.1.11:8443")
    _enroll_host(client, admin_token, name="node-b", address="10.0.1.12:8443")
    _create_running_world(client, operator_token)

    drain = client.post("/api/v1/hosts/node-b/drain", headers=auth_header(operator_token))
    assert drain.status_code == 200

    resp = client.post(
        "/api/v1/worlds/world-overworld/migrate",
        params={"target_host": "node-b"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 409


def test_migrate_rejects_insufficient_target_capacity(client, admin_token, operator_token):
    _enroll_host(client, admin_token, name="node-a", address="10.0.1.11:8443", capacity={"cpu_cores": 8, "memory_gb": 16})
    _enroll_host(client, admin_token, name="node-b", address="10.0.1.12:8443", capacity={"cpu_cores": 2, "memory_gb": 2})
    _create_running_world(client, operator_token, cpu_cores=4, memory_gb=8)

    resp = client.post(
        "/api/v1/worlds/world-overworld/migrate",
        params={"target_host": "node-b"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 409


def test_migrate_requires_placed_world(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "nether", "cpu_cores": 2, "memory_gb": 3},
        headers=auth_header(operator_token),
    )
    resp = client.post(
        "/api/v1/worlds/world-stuck/migrate",
        params={"target_host": "node-a"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 409


def test_migrate_failure_leaves_world_on_source_host(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token, name="node-a", address="10.0.1.11:8443")
    _enroll_host(client, admin_token, name="node-b", address="10.0.1.12:8443")
    _create_running_world(client, operator_token, "world-overworld")

    fake_lxd.fail_migrate_for.add("world-overworld")

    resp = client.post(
        "/api/v1/worlds/world-overworld/migrate",
        params={"target_host": "node-b"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 502

    worlds = client.get("/api/v1/worlds", headers=auth_header(operator_token)).json()
    world = next(w for w in worlds if w["name"] == "world-overworld")
    assert world["host_name"] == "node-a"
    assert ("node-a", "world-overworld") not in fake_lxd.deleted
