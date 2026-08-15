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


def _create_running_world(client, operator_token, name="world-overworld"):
    resp = client.post(
        "/api/v1/worlds",
        json={"name": name, "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    assert resp.json()["phase"] == "running"
    return resp.json()


def _world_phase(client, operator_token, name):
    worlds = client.get("/api/v1/worlds", headers=auth_header(operator_token)).json()
    return next(w for w in worlds if w["name"] == name)["phase"]


def _trigger_reconcile(client, operator_token, trigger_name):
    """A world create is the simplest way to force one reconcile pass
    through the API surface under test (matches how a real operator
    action, not just the 15s background loop, gets a crash noticed)."""
    resp = client.post(
        "/api/v1/worlds",
        json={"name": trigger_name, "type": "lobby", "cpu_cores": 1, "memory_gb": 1},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200


def test_unhealthy_running_world_is_recovered_across_two_reconciles(
    client, admin_token, operator_token, fake_lxd, fake_health_check
):
    _enroll_host(client, admin_token)
    _create_running_world(client, operator_token, "world-overworld")

    fake_health_check.unhealthy.add("world-overworld")

    # Pass 1: check_running_worlds marks it crashed, recover_crashed_worlds
    # restarts the container and moves it to provisioning in the same pass
    # (that ordering is deliberate — see scheduler.reconcile).
    _trigger_reconcile(client, operator_token, "world-trigger-1")
    assert _world_phase(client, operator_token, "world-overworld") == "provisioning"
    assert ("node-a", "world-overworld") in fake_lxd.restarted

    # It's no longer "running", so it won't be re-health-checked while
    # recovering — stop flagging it unhealthy, matching a real recovered JVM.
    fake_health_check.unhealthy.discard("world-overworld")

    # Pass 2: finalize_provisioning re-polls for an address and flips it
    # back to running.
    _trigger_reconcile(client, operator_token, "world-trigger-2")
    assert _world_phase(client, operator_token, "world-overworld") == "running"


def test_healthy_world_is_never_touched(client, admin_token, operator_token, fake_lxd, fake_health_check):
    _enroll_host(client, admin_token)
    _create_running_world(client, operator_token, "world-overworld")

    assert "world-overworld" in fake_health_check.calls
    assert ("node-a", "world-overworld") not in fake_lxd.restarted
