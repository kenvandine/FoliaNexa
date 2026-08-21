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


def test_unhealthy_running_world_is_recovered_across_two_reconciles(
    client, admin_token, operator_token, fake_lxd, fake_health_check, trigger_reconcile
):
    _enroll_host(client, admin_token)
    _create_running_world(client, operator_token, "world-overworld")

    fake_health_check.unhealthy.add("world-overworld")

    # Pass 1: check_running_worlds marks it crashed, recover_crashed_worlds
    # restarts the container and moves it to provisioning in the same pass
    # (that ordering is deliberate — see scheduler.reconcile).
    trigger_reconcile()
    assert _world_phase(client, operator_token, "world-overworld") == "provisioning"
    assert ("node-a", "world-overworld") in fake_lxd.restarted

    # It's no longer "running", so it won't be re-health-checked while
    # recovering — stop flagging it unhealthy, matching a real recovered JVM.
    fake_health_check.unhealthy.discard("world-overworld")

    # Pass 2: finalize_provisioning re-polls for an address and flips it
    # back to running.
    trigger_reconcile()
    assert _world_phase(client, operator_token, "world-overworld") == "running"


def test_healthy_world_is_never_touched(
    client, admin_token, operator_token, fake_lxd, fake_health_check, trigger_reconcile
):
    _enroll_host(client, admin_token)
    _create_running_world(client, operator_token, "world-overworld")

    # World creation itself no longer health-checks the whole cluster (see
    # trigger_reconcile's docstring) — force one real pass to confirm a
    # healthy world sails through check_running_worlds untouched.
    trigger_reconcile()
    assert "world-overworld" in fake_health_check.calls
    assert ("node-a", "world-overworld") not in fake_lxd.restarted


def test_unreachable_crashed_world_does_not_block_other_reconcile_steps(
    client, admin_token, operator_token, fake_lxd, fake_health_check, trigger_reconcile
):
    # Regression test for a live incident: one world stuck in `crashed`
    # phase on an unreachable host made recover_crashed_worlds's
    # restart_container raise uncaught (a transport-level failure, not the
    # LXDError that function's own try/except is written for) — which
    # aborted the entire reconcile() call before it ever reached
    # sync_stats_configs, silently stalling FoliaNexaStats token
    # provisioning (and whitelist/LuckPerms sync) for every OTHER world in
    # the cluster too, for as long as that one host stayed down.
    # Default host capacity (6 cpu / 12gb) only fits one 4cpu/8gb world —
    # bump it so both worlds actually land on node-a and go `running`.
    _enroll_host(client, admin_token, capacity={"cpu_cores": 12, "memory_gb": 24})
    _create_running_world(client, operator_token, "world-crashed")
    _create_running_world(client, operator_token, "world-stats")

    fake_health_check.unhealthy.add("world-crashed")
    # Simulate the unreachable-host failure mode itself: a raw transport
    # exception (not LXDError) out of restart_container. Set before
    # triggering — check_running_worlds and recover_crashed_worlds both run
    # within the same reconcile() tick (see the first test in this file),
    # so world-crashed gets marked crashed and its restart attempted in one
    # call.
    fake_lxd.fail_restart_with["world-crashed"] = ConnectionError("simulated unreachable host")

    trigger_reconcile()

    # The crashed world's own recovery attempt failed, as expected — this
    # isn't asserting the crash away, only that it didn't take anything
    # else down with it.
    assert _world_phase(client, operator_token, "world-crashed") == "crashed"

    # The real assertion: world-stats' FoliaNexaStats config still got
    # pushed this same tick, via sync_stats_configs — which runs after
    # recover_crashed_worlds in reconcile() — despite the unrelated
    # world's restart blowing up first.
    assert any(
        key[1] == "world-stats" and key[2].endswith("FoliaNexaStats/config.yml")
        for key in fake_lxd.pushed_files
    )
