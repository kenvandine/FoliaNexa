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


def test_world_stays_pending_with_no_capacity(client, operator_token):
    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-nether", "type": "nether", "cpu_cores": 2, "memory_gb": 3},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phase"] == "pending"
    assert resp.json()["host_name"] is None


def test_world_gets_placed_and_becomes_running(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)

    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "running"
    assert body["host_name"] == "node-a"
    assert body["address"] is not None
    assert ("node-a", "world-overworld") in fake_lxd.launched


def test_world_respects_placement_labels(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token, name="node-e", address="10.0.1.20:8443", labels={"cpu_type": "e-core"})

    resp = client.post(
        "/api/v1/worlds",
        json={
            "name": "world-lobby",
            "type": "lobby",
            "cpu_cores": 1,
            "memory_gb": 1,
            "placement_labels": {"cpu_type": "p-core"},
        },
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    # no host advertises cpu_type=p-core, so it should stay pending rather
    # than land on the mismatched e-core host
    assert resp.json()["phase"] == "pending"
    assert fake_lxd.launched == []


def test_world_bin_packing_prefers_most_free_capacity(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token, name="node-big", address="10.0.1.30:8443",
                 capacity={"cpu_cores": 8, "memory_gb": 16})
    _enroll_host(client, admin_token, name="node-small", address="10.0.1.31:8443",
                 capacity={"cpu_cores": 2, "memory_gb": 4})

    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-minigame", "type": "minigame", "cpu_cores": 1, "memory_gb": 1},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    # both hosts fit, but node-big has more free capacity remaining after
    # placement -> fullest-fit-remaining should pick it
    assert resp.json()["host_name"] == "node-big"


def test_delete_world_tears_down_container(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.delete("/api/v1/worlds/world-overworld", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["phase"] == "deleted"
    assert ("node-a", "world-overworld") in fake_lxd.deleted


def test_snapshot_requires_placed_world(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "nether", "cpu_cores": 2, "memory_gb": 3},
        headers=auth_header(operator_token),
    )
    resp = client.post("/api/v1/worlds/world-stuck/snapshot", headers=auth_header(operator_token))
    assert resp.status_code == 409


def test_snapshot_running_world(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.post(
        "/api/v1/worlds/world-overworld/snapshot",
        params={"snapshot_name": "pre-plugin-test"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["snapshot"] == "pre-plugin-test"
    assert ("node-a", "world-overworld", "pre-plugin-test") in fake_lxd.snapshots


def test_migrate_not_implemented(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    resp = client.post(
        "/api/v1/worlds/world-overworld/migrate",
        params={"target_host": "node-b"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 501


def test_world_access_update(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.put(
        "/api/v1/worlds/world-overworld/access",
        json={"whitelist_enabled": True, "ops": ["ken"]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"whitelist_enabled": True, "ops": ["ken"]}

    get_resp = client.get("/api/v1/worlds/world-overworld/access", headers=auth_header(operator_token))
    assert get_resp.json() == {"whitelist_enabled": True, "ops": ["ken"]}


def test_routes_only_lists_running_routable_worlds(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.post(
        "/api/v1/worlds",
        json={"name": "world-nether", "type": "nether", "cpu_cores": 1, "memory_gb": 1},
        headers=auth_header(operator_token),
    )
    # stays pending: node-a's remaining capacity after the two placements
    # above (1 cpu / 3 gb) isn't enough for this one
    client.post(
        "/api/v1/worlds",
        json={"name": "world-huge", "type": "minigame", "cpu_cores": 10, "memory_gb": 10},
        headers=auth_header(operator_token),
    )

    resp = client.get("/api/v1/routes", headers=auth_header(operator_token))
    assert resp.status_code == 200
    names = {r["world"] for r in resp.json()["routes"]}
    assert names == {"world-overworld", "world-nether"}
    default_routes = [r for r in resp.json()["routes"] if r["default"]]
    assert len(default_routes) == 1
    assert default_routes[0]["world"] == "world-overworld"
