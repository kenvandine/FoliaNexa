from __future__ import annotations

from helpers import auth_header


def _enroll_host(client, admin_token):
    join = client.post("/api/v1/hosts/join-token", headers=auth_header(admin_token)).json()["token"]
    body = {
        "name": "node-a",
        "address": "10.0.1.11:8443",
        "project": "folia",
        "lxd_trust_token": "good-token",
        "capacity": {"cpu_cores": 6, "memory_gb": 12},
        "labels": {},
    }
    resp = client.post("/api/v1/hosts/enroll", json=body, headers=auth_header(join))
    assert resp.status_code == 200, resp.text


def test_get_minecraft_version_defaults(client, viewer_token):
    resp = client.get("/api/v1/cluster/minecraft-version", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    assert resp.json() == {"engine": "folia", "version": "26.2"}


def test_get_minecraft_version_requires_auth(client):
    resp = client.get("/api/v1/cluster/minecraft-version")
    assert resp.status_code == 401


def test_update_minecraft_version_requires_operator(client, viewer_token):
    resp = client.put(
        "/api/v1/cluster/minecraft-version", json={"version": "26.3"}, headers=auth_header(viewer_token)
    )
    assert resp.status_code == 403


def test_update_and_read_back_minecraft_version(client, operator_token, viewer_token):
    resp = client.put(
        "/api/v1/cluster/minecraft-version",
        json={"engine": "folia", "version": "26.3"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"engine": "folia", "version": "26.3"}

    resp = client.get("/api/v1/cluster/minecraft-version", headers=auth_header(viewer_token))
    assert resp.json() == {"engine": "folia", "version": "26.3"}


def test_new_world_snapshots_current_cluster_version(client, admin_token, operator_token, fake_lxd):
    client.put(
        "/api/v1/cluster/minecraft-version", json={"version": "26.3"}, headers=auth_header(operator_token)
    )
    _enroll_host(client, admin_token)
    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "26.3"
    assert resp.json()["engine"] == "folia"


def test_changing_cluster_version_does_not_retroactively_touch_existing_worlds(
    client, admin_token, operator_token, fake_lxd
):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.put(
        "/api/v1/cluster/minecraft-version", json={"version": "26.3"}, headers=auth_header(operator_token)
    )

    resp = client.get("/api/v1/worlds", headers=auth_header(operator_token))
    world = next(w for w in resp.json() if w["name"] == "world-overworld")
    assert world["version"] == "26.2"  # unchanged until an explicit migrate


def test_migrate_pushes_new_version_and_restarts_running_worlds(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.put(
        "/api/v1/cluster/minecraft-version", json={"version": "26.3"}, headers=auth_header(operator_token)
    )

    resp = client.post("/api/v1/cluster/minecraft-version/migrate", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["engine"] == "folia"
    assert body["version"] == "26.3"
    assert body["results"] == [{"world": "world-overworld", "migrated": True, "detail": None}]

    assert ("node-a", "world-overworld") in fake_lxd.restarted
    pushed = fake_lxd.updated_config[("node-a", "world-overworld")]
    assert pushed["user.folia.jar-version"] == "26.3"

    world = client.get("/api/v1/worlds", headers=auth_header(operator_token)).json()[0]
    assert world["version"] == "26.3"


def test_migrate_skips_unplaced_worlds(client, operator_token, fake_lxd):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "overworld", "cpu_cores": 999, "memory_gb": 999},
        headers=auth_header(operator_token),
    )
    resp = client.post("/api/v1/cluster/minecraft-version/migrate", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []  # world-stuck is "pending", not "running" — never even considered


def test_migrate_requires_operator(client, viewer_token):
    resp = client.post("/api/v1/cluster/minecraft-version/migrate", headers=auth_header(viewer_token))
    assert resp.status_code == 403
