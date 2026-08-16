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


def test_patch_updates_plugins_and_merges_catalog_defaults(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8, "plugins": ["Spark"]},
        headers=auth_header(operator_token),
    )
    resp = client.patch(
        "/api/v1/worlds/world-overworld",
        json={"plugins": ["LuckPerms"]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plugins"] == ["LuckPerms", "FoliaNexaStats", "HuskHomes"]


def test_patch_updates_datapacks_and_properties_independently(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    resp = client.patch(
        "/api/v1/worlds/world-overworld",
        json={"datapacks": ["Matcha"], "properties": {"allow-flight": "true", "pvp": "false"}},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["datapacks"] == ["Matcha"]
    assert body["properties"] == {"allow-flight": "true", "pvp": "false"}
    # plugins untouched (defaults were already merged in at creation)
    assert body["plugins"] == ["FoliaNexaStats", "HuskHomes"]


def test_patch_omitted_fields_leave_existing_values_untouched(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={
            "name": "world-overworld",
            "type": "overworld",
            "cpu_cores": 4,
            "memory_gb": 8,
            "datapacks": ["Matcha"],
            "properties": {"pvp": "false"},
        },
        headers=auth_header(operator_token),
    )
    resp = client.patch(
        "/api/v1/worlds/world-overworld",
        json={"properties": {"pvp": "false", "difficulty": "hard"}},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["datapacks"] == ["Matcha"]
    assert resp.json()["properties"] == {"pvp": "false", "difficulty": "hard"}


def test_patch_rejects_protected_properties(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    resp = client.patch(
        "/api/v1/worlds/world-overworld",
        json={"properties": {"online-mode": "true"}},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 400
    assert "online-mode" in resp.json()["detail"]


def test_patch_rejects_unknown_plugin(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    resp = client.patch(
        "/api/v1/worlds/world-overworld",
        json={"plugins": ["NotARealPlugin"]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 400
    assert "NotARealPlugin" in resp.text


def test_patch_requires_operator_role(client, viewer_token):
    resp = client.patch(
        "/api/v1/worlds/no-such-world",
        json={"properties": {"pvp": "false"}},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


def test_patch_404s_for_unknown_world(client, operator_token):
    resp = client.patch(
        "/api/v1/worlds/no-such-world",
        json={"properties": {"pvp": "false"}},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 404


def test_patch_pushes_updated_config_to_a_placed_world(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    resp = client.patch(
        "/api/v1/worlds/world-overworld",
        json={"properties": {"pvp": "false"}},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert ("node-a", "world-overworld") in fake_lxd.updated_config
    pushed = fake_lxd.updated_config[("node-a", "world-overworld")]
    assert pushed["user.folia.server-properties-manifest-url"].endswith(
        "/api/v1/worlds/world-overworld/server-properties-manifest"
    )


def test_patch_does_not_push_config_for_an_unplaced_world(client, operator_token, fake_lxd):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "overworld", "cpu_cores": 999, "memory_gb": 999},
        headers=auth_header(operator_token),
    )
    resp = client.patch(
        "/api/v1/worlds/world-stuck",
        json={"properties": {"pvp": "false"}},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert fake_lxd.updated_config == {}


def test_server_properties_manifest_reflects_world_properties(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={
            "name": "world-overworld",
            "type": "overworld",
            "cpu_cores": 4,
            "memory_gb": 8,
            "properties": {"allow-flight": "true"},
        },
        headers=auth_header(operator_token),
    )
    resp = client.get("/api/v1/worlds/world-overworld/server-properties-manifest")
    assert resp.status_code == 200
    assert resp.json() == {"allow-flight": "true"}


def test_server_properties_manifest_empty_by_default(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    resp = client.get("/api/v1/worlds/world-overworld/server-properties-manifest")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_server_properties_manifest_404s_for_unknown_world(client):
    resp = client.get("/api/v1/worlds/no-such-world/server-properties-manifest")
    assert resp.status_code == 404


def test_restart_world_calls_lxd_restart(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    resp = client.post("/api/v1/worlds/world-overworld/restart", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert ("node-a", "world-overworld") in fake_lxd.restarted


def test_restart_requires_placed_world(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "overworld", "cpu_cores": 999, "memory_gb": 999},
        headers=auth_header(operator_token),
    )
    resp = client.post("/api/v1/worlds/world-stuck/restart", headers=auth_header(operator_token))
    assert resp.status_code == 409


def test_restart_requires_operator_role(client, viewer_token):
    resp = client.post("/api/v1/worlds/no-such-world/restart", headers=auth_header(viewer_token))
    assert resp.status_code == 403
