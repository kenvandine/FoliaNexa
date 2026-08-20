from __future__ import annotations

from helpers import auth_header
from sqlmodel import select
from test_rcon import FakeMinecraftRconServer

from folia_mgmt.models import World


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


def test_restart_pushes_config_to_the_container(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    fake_lxd.updated_config.clear()  # placement itself already pushed config once
    resp = client.post("/api/v1/worlds/world-overworld/restart", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert ("node-a", "world-overworld") in fake_lxd.updated_config
    pushed = fake_lxd.updated_config[("node-a", "world-overworld")]
    assert pushed["user.folia.rcon-password"]


def test_restart_backfills_rcon_password_for_world_declared_before_it_existed(
    client, admin_token, operator_token, fake_lxd, db_session
):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    world = db_session.exec(select(World).where(World.name == "world-overworld")).one()
    world.rcon_password = None
    db_session.add(world)
    db_session.commit()

    resp = client.post("/api/v1/worlds/world-overworld/restart", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    world = db_session.exec(select(World).where(World.name == "world-overworld")).one()
    assert world.rcon_password
    pushed = fake_lxd.updated_config[("node-a", "world-overworld")]
    assert pushed["user.folia.rcon-password"] == world.rcon_password


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


def test_stop_world_calls_lxd_stop_and_clears_address(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    resp = client.post("/api/v1/worlds/world-overworld/stop", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert ("node-a", "world-overworld") in fake_lxd.stopped

    world = next(w for w in client.get("/api/v1/worlds", headers=auth_header(operator_token)).json()
                 if w["name"] == "world-overworld")
    assert world["phase"] == "stopped"
    assert world["address"] is None


def test_stop_requires_running_or_crashed_world(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.post("/api/v1/worlds/world-overworld/stop", headers=auth_header(operator_token))
    # Already stopped — a second stop is a conflict, not a silent no-op.
    resp = client.post("/api/v1/worlds/world-overworld/stop", headers=auth_header(operator_token))
    assert resp.status_code == 409


def test_stop_requires_placed_world(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "overworld", "cpu_cores": 999, "memory_gb": 999},
        headers=auth_header(operator_token),
    )
    resp = client.post("/api/v1/worlds/world-stuck/stop", headers=auth_header(operator_token))
    assert resp.status_code == 409


def test_stop_requires_operator_role(client, viewer_token):
    resp = client.post("/api/v1/worlds/no-such-world/stop", headers=auth_header(viewer_token))
    assert resp.status_code == 403


def test_start_world_calls_lxd_start_and_becomes_running(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.post("/api/v1/worlds/world-overworld/stop", headers=auth_header(operator_token))

    resp = client.post("/api/v1/worlds/world-overworld/start", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert ("node-a", "world-overworld") in fake_lxd.started

    world = next(w for w in client.get("/api/v1/worlds", headers=auth_header(operator_token)).json()
                 if w["name"] == "world-overworld")
    # finalize_provisioning runs immediately (fake LXD returns an address
    # right away), so this reaches 'running' in the same request.
    assert world["phase"] == "running"
    assert world["address"]


def test_start_requires_stopped_world(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    # Currently running, not stopped.
    resp = client.post("/api/v1/worlds/world-overworld/start", headers=auth_header(operator_token))
    assert resp.status_code == 409


def test_start_requires_operator_role(client, viewer_token):
    resp = client.post("/api/v1/worlds/no-such-world/start", headers=auth_header(viewer_token))
    assert resp.status_code == 403


def test_create_world_generates_rcon_password_not_exposed_in_response(client, operator_token, db_session):
    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert "rcon_password" not in resp.json()

    world = db_session.exec(select(World).where(World.name == "world-overworld")).one()
    assert world.rcon_password  # generated, non-empty


def test_patch_backfills_rcon_password_for_world_declared_before_it_existed(client, operator_token, db_session):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    world = db_session.exec(select(World).where(World.name == "world-overworld")).one()
    world.rcon_password = None
    db_session.add(world)
    db_session.commit()

    client.patch(
        "/api/v1/worlds/world-overworld",
        json={"properties": {"pvp": "false"}},
        headers=auth_header(operator_token),
    )
    db_session.refresh(world)
    assert world.rcon_password


def test_node_config_includes_rcon_password_when_set(db_session):
    from folia_mgmt.config import Settings
    from folia_mgmt.models import World, WorldType
    from folia_mgmt.scheduler import _node_config

    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8, rcon_password="s3cr3t")
    config = _node_config(db_session, world, Settings(public_url=None))
    assert config["user.folia.rcon-password"] == "s3cr3t"


def test_node_config_omits_rcon_password_when_unset(db_session):
    from folia_mgmt.config import Settings
    from folia_mgmt.models import World, WorldType
    from folia_mgmt.scheduler import _node_config

    world = World(name="w", type=WorldType.overworld, cpu_cores=4, memory_gb=8)
    config = _node_config(db_session, world, Settings(public_url=None))
    assert "user.folia.rcon-password" not in config


def test_rcon_command_runs_against_a_real_fake_rcon_server(client, operator_token, db_session):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    server = FakeMinecraftRconServer(password="whatever", command_responses={"time set day": "Set the time to 1000"})
    try:
        world = db_session.exec(select(World).where(World.name == "world-overworld")).one()
        world.address = f"127.0.0.1:25565"
        world.rcon_password = "whatever"
        db_session.add(world)
        db_session.commit()

        import folia_mgmt.routers.worlds as worlds_router

        original_port = worlds_router.RCON_PORT
        worlds_router.RCON_PORT = server.port
        try:
            resp = client.post(
                "/api/v1/worlds/world-overworld/rcon",
                json={"command": "time set day"},
                headers=auth_header(operator_token),
            )
        finally:
            worlds_router.RCON_PORT = original_port
    finally:
        server.close()

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"response": "Set the time to 1000"}
    assert server.received_commands == ["time set day"]


def test_rcon_requires_placed_world(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "overworld", "cpu_cores": 999, "memory_gb": 999},
        headers=auth_header(operator_token),
    )
    resp = client.post(
        "/api/v1/worlds/world-stuck/rcon", json={"command": "list"}, headers=auth_header(operator_token)
    )
    assert resp.status_code == 409


def test_rcon_requires_operator_role(client, viewer_token):
    resp = client.post(
        "/api/v1/worlds/no-such-world/rcon", json={"command": "list"}, headers=auth_header(viewer_token)
    )
    assert resp.status_code == 403
