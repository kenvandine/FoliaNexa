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


def test_create_world_rejects_unknown_plugin(client, operator_token):
    resp = client.post(
        "/api/v1/worlds",
        json={
            "name": "world-overworld",
            "type": "overworld",
            "cpu_cores": 4,
            "memory_gb": 8,
            "plugins": ["LuckPerms", "TotallyMadeUpPlugin"],
        },
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 400
    assert "TotallyMadeUpPlugin" in resp.text


def test_create_world_accepts_catalog_plugins(client, operator_token):
    resp = client.post(
        "/api/v1/worlds",
        json={
            "name": "world-overworld",
            "type": "overworld",
            "cpu_cores": 4,
            "memory_gb": 8,
            "plugins": ["LuckPerms", "Spark"],
        },
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plugins"] == ["LuckPerms", "Spark"]


def test_create_world_with_plugins_requires_public_url(client, operator_token, app):
    from folia_mgmt.config import Settings
    from folia_mgmt.deps import settings_dependency

    no_public_url_settings = Settings(state_dir=app.state.test_settings.state_dir, public_url=None)
    app.dependency_overrides[settings_dependency] = lambda: no_public_url_settings
    try:
        resp = client.post(
            "/api/v1/worlds",
            json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8, "plugins": ["LuckPerms"]},
            headers=auth_header(operator_token),
        )
        assert resp.status_code == 400
        assert "PUBLIC_URL" in resp.text
    finally:
        del app.dependency_overrides[settings_dependency]


def test_create_world_without_plugins_does_not_need_public_url(client, operator_token, app):
    from folia_mgmt.config import Settings
    from folia_mgmt.deps import settings_dependency

    no_public_url_settings = Settings(state_dir=app.state.test_settings.state_dir, public_url=None)
    app.dependency_overrides[settings_dependency] = lambda: no_public_url_settings
    try:
        resp = client.post(
            "/api/v1/worlds",
            json={"name": "world-lobby", "type": "lobby", "cpu_cores": 1, "memory_gb": 1},
            headers=auth_header(operator_token),
        )
        assert resp.status_code == 200, resp.text
    finally:
        del app.dependency_overrides[settings_dependency]


def test_plugins_manifest_generated_from_catalog(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    create = client.post(
        "/api/v1/worlds",
        json={
            "name": "world-overworld",
            "type": "overworld",
            "cpu_cores": 4,
            "memory_gb": 8,
            "plugins": ["LuckPerms", "Spark"],
        },
        headers=auth_header(operator_token),
    )
    assert create.status_code == 200, create.text

    resp = client.get("/api/v1/worlds/world-overworld/plugins-manifest")
    assert resp.status_code == 200, resp.text
    manifest = resp.json()
    names = {entry["name"] for entry in manifest}
    assert names == {"LuckPerms", "Spark"}
    for entry in manifest:
        assert entry["url"].startswith("http")


def test_plugins_manifest_is_unauthenticated(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8, "plugins": ["LuckPerms"]},
        headers=auth_header(operator_token),
    )
    # no Authorization header at all — node has no mgmt credential
    resp = client.get("/api/v1/worlds/world-overworld/plugins-manifest")
    assert resp.status_code == 200


def test_plugins_manifest_skips_entries_with_no_download_url(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    # HuskClaims is a real catalog entry but has download_url: null (unverified placeholder)
    client.post(
        "/api/v1/worlds",
        json={
            "name": "world-overworld",
            "type": "overworld",
            "cpu_cores": 4,
            "memory_gb": 8,
            "plugins": ["LuckPerms", "HuskClaims"],
        },
        headers=auth_header(operator_token),
    )
    resp = client.get("/api/v1/worlds/world-overworld/plugins-manifest")
    names = {entry["name"] for entry in resp.json()}
    assert names == {"LuckPerms"}  # HuskClaims silently dropped, not a 500


def test_plugins_manifest_empty_for_world_without_plugins(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-lobby", "type": "lobby", "cpu_cores": 1, "memory_gb": 1},
        headers=auth_header(operator_token),
    )
    resp = client.get("/api/v1/worlds/world-lobby/plugins-manifest")
    assert resp.status_code == 200
    assert resp.json() == []


def test_plugins_manifest_404s_for_unknown_world(client):
    resp = client.get("/api/v1/worlds/no-such-world/plugins-manifest")
    assert resp.status_code == 404
