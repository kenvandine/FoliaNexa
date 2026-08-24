from __future__ import annotations

from sqlmodel import select

from folia_mgmt.access_apply import NODE_WORLD_DIR
from folia_mgmt.lxd_client import LXDError
from folia_mgmt.models import World, utcnow
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


def test_deleted_world_is_gone_not_just_soft_deleted(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.delete("/api/v1/worlds/world-overworld", headers=auth_header(operator_token))

    resp = client.get("/api/v1/worlds", headers=auth_header(operator_token))
    assert "world-overworld" not in {w["name"] for w in resp.json()}


def test_world_name_can_be_reused_after_deletion(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.delete("/api/v1/worlds/world-overworld", headers=auth_header(operator_token))

    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "minigame", "cpu_cores": 2, "memory_gb": 4},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "minigame"
    assert resp.json()["phase"] != "deleted"


def test_snapshot_disabled_by_default(client, admin_token, operator_token, fake_lxd):
    # The ad-hoc LXD-instance-snapshot endpoints are kept as a feature
    # but off by default (Settings.lxd_snapshot_backups_enabled) — the
    # tracked "time machine" backup feature never depends on this flag,
    # only this older, lower-level escape hatch does.
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.post("/api/v1/worlds/world-overworld/snapshot", headers=auth_header(operator_token))
    assert resp.status_code == 403
    assert fake_lxd.snapshots == []


def test_snapshot_requires_placed_world(client, operator_token, monkeypatch):
    monkeypatch.setenv("FOLIA_MGMT_LXD_SNAPSHOT_BACKUPS_ENABLED", "true")
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "nether", "cpu_cores": 2, "memory_gb": 3},
        headers=auth_header(operator_token),
    )
    resp = client.post("/api/v1/worlds/world-stuck/snapshot", headers=auth_header(operator_token))
    assert resp.status_code == 409


def test_snapshot_running_world(client, admin_token, operator_token, fake_lxd, monkeypatch):
    monkeypatch.setenv("FOLIA_MGMT_LXD_SNAPSHOT_BACKUPS_ENABLED", "true")
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


def test_routes_prefers_lobby_over_overworld_as_default(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.post(
        "/api/v1/worlds",
        json={"name": "world-lobby", "type": "lobby", "cpu_cores": 1, "memory_gb": 1},
        headers=auth_header(operator_token),
    )

    resp = client.get("/api/v1/routes", headers=auth_header(operator_token))
    assert resp.status_code == 200
    default_routes = [r for r in resp.json()["routes"] if r["default"]]
    assert len(default_routes) == 1
    assert default_routes[0]["world"] == "world-lobby"


def test_new_world_defaults_to_backups_enabled(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["backups_enabled"] is True


def test_put_backups_config_toggles_flag(client, admin_token, operator_token, viewer_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.put(
        "/api/v1/worlds/world-overworld/backups-config",
        json={"enabled": False},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["backups_enabled"] is False

    worlds = client.get("/api/v1/worlds", headers=auth_header(operator_token)).json()
    assert next(w for w in worlds if w["name"] == "world-overworld")["backups_enabled"] is False

    # viewer can't toggle it
    resp = client.put(
        "/api/v1/worlds/world-overworld/backups-config",
        json={"enabled": True},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


def test_disabling_backups_clears_a_stale_failure(client, admin_token, operator_token, db_session):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    world = db_session.exec(select(World).where(World.name == "world-overworld")).one()
    world.last_backup_error = "stale failure from a previous tick"
    world.last_backup_attempt_at = utcnow()
    db_session.add(world)
    db_session.commit()

    resp = client.put(
        "/api/v1/worlds/world-overworld/backups-config",
        json={"enabled": False},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text

    worlds = client.get("/api/v1/worlds", headers=auth_header(operator_token)).json()
    world_body = next(w for w in worlds if w["name"] == "world-overworld")
    assert world_body["last_backup_error"] is None
    assert world_body["last_backup_attempt_at"] is None


def test_viewer_role_cannot_see_last_backup_error(client, admin_token, operator_token, viewer_token, db_session):
    """last_backup_error carries raw LXDError text (internal backend
    detail) — a viewer-role token should still see backups_enabled and
    last_backup_attempt_at, but not the error message itself."""
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    world = db_session.exec(select(World).where(World.name == "world-overworld")).one()
    world.last_backup_error = "raw LXD error detail"
    world.last_backup_attempt_at = utcnow()
    db_session.add(world)
    db_session.commit()

    viewer_worlds = client.get("/api/v1/worlds", headers=auth_header(viewer_token)).json()
    viewer_body = next(w for w in viewer_worlds if w["name"] == "world-overworld")
    assert viewer_body["last_backup_error"] is None
    assert viewer_body["last_backup_attempt_at"] is not None

    operator_worlds = client.get("/api/v1/worlds", headers=auth_header(operator_token)).json()
    operator_body = next(w for w in operator_worlds if w["name"] == "world-overworld")
    assert operator_body["last_backup_error"] == "raw LXD error detail"


def test_list_backups_empty_for_new_world(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.get("/api/v1/worlds/world-overworld/backups", headers=auth_header(operator_token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_scheduled_backup_appears_in_list_after_reconcile(client, admin_token, operator_token, fake_world_backups, trigger_reconcile):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    trigger_reconcile()

    resp = client.get("/api/v1/worlds/world-overworld/backups", headers=auth_header(operator_token))
    assert resp.status_code == 200
    backups = resp.json()
    assert len(backups) == 1
    assert backups[0]["kind"] == "scheduled"
    assert backups[0]["size_bytes"] is not None and backups[0]["size_bytes"] > 0
    assert ("world-overworld", backups[0]["snapshot_name"]) in fake_world_backups.fetched


def test_disabled_world_gets_no_scheduled_backup(client, admin_token, operator_token, fake_world_backups, trigger_reconcile):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.put(
        "/api/v1/worlds/world-overworld/backups-config",
        json={"enabled": False},
        headers=auth_header(operator_token),
    )

    trigger_reconcile()

    resp = client.get("/api/v1/worlds/world-overworld/backups", headers=auth_header(operator_token))
    assert resp.status_code == 200
    assert resp.json() == []
    assert fake_world_backups.fetched == []


def test_manual_backup_creates_a_backup_immediately(client, admin_token, operator_token, fake_world_backups):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.post(
        "/api/v1/worlds/world-overworld/backups/manual", headers=auth_header(operator_token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "manual"
    assert body["snapshot_name"].startswith("manual-")
    assert body["size_bytes"] is not None and body["size_bytes"] > 0
    assert ("world-overworld", body["snapshot_name"]) in fake_world_backups.fetched

    backups = client.get(
        "/api/v1/worlds/world-overworld/backups", headers=auth_header(operator_token)
    ).json()
    assert len(backups) == 1
    assert backups[0]["kind"] == "manual"


def test_manual_backup_label_does_not_collide_with_ad_hoc_snapshot_or_itself(
    client, admin_token, operator_token
):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    # Two manual backups back-to-back (e.g. a double-clicked "Back up
    # now" button) must not collide even within the same wall-clock
    # second, and the label must not match the older ad-hoc
    # POST /{name}/snapshot endpoint's plain "manual-<epoch>" format,
    # since LXD rejects a duplicate snapshot name for the same container.
    first = client.post(
        "/api/v1/worlds/world-overworld/backups/manual", headers=auth_header(operator_token)
    )
    second = client.post(
        "/api/v1/worlds/world-overworld/backups/manual", headers=auth_header(operator_token)
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    first_label = first.json()["snapshot_name"]
    second_label = second.json()["snapshot_name"]
    assert first_label != second_label
    assert first_label.startswith("manual-backup-")
    assert second_label.startswith("manual-backup-")


def test_manual_backup_works_even_when_automatic_backups_disabled(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.put(
        "/api/v1/worlds/world-overworld/backups-config",
        json={"enabled": False},
        headers=auth_header(operator_token),
    )

    resp = client.post(
        "/api/v1/worlds/world-overworld/backups/manual", headers=auth_header(operator_token)
    )
    assert resp.status_code == 200, resp.text


def test_manual_backup_requires_operator(client, admin_token, operator_token, viewer_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.post(
        "/api/v1/worlds/world-overworld/backups/manual", headers=auth_header(viewer_token)
    )
    assert resp.status_code == 403


def test_manual_backup_requires_placed_world(client, operator_token):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "nether", "cpu_cores": 2, "memory_gb": 3},
        headers=auth_header(operator_token),
    )
    resp = client.post(
        "/api/v1/worlds/world-stuck/backups/manual", headers=auth_header(operator_token)
    )
    assert resp.status_code == 409


def test_manual_backup_requires_existing_world(client, operator_token):
    resp = client.post(
        "/api/v1/worlds/no-such-world/backups/manual", headers=auth_header(operator_token)
    )
    assert resp.status_code == 404


def test_restore_backup_requires_admin(client, admin_token, operator_token, fake_lxd, trigger_reconcile):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    trigger_reconcile()
    backup_id = client.get(
        "/api/v1/worlds/world-overworld/backups", headers=auth_header(operator_token)
    ).json()[0]["id"]

    # operator is not enough — this is the "time machine" restore, admin-only
    resp = client.post(
        f"/api/v1/worlds/world-overworld/backups/{backup_id}/restore",
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 403

    resp = client.post(
        f"/api/v1/worlds/world-overworld/backups/{backup_id}/restore",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["restoring"]
    assert ("node-a", "world-overworld") in fake_lxd.restarted
    pushed_paths = [path for (h, n, path) in fake_lxd.pushed_files if (h, n) == ("node-a", "world-overworld")]
    assert any(path.endswith(".pending-restore.tar.gz") for path in pushed_paths)


def test_restore_backup_409s_when_a_restore_is_already_in_flight(
    client, admin_token, operator_token, fake_lxd, trigger_reconcile
):
    # lxd_client.restore_guard rejects a second concurrent restore of the
    # same world before ever touching the network — a double-clicked
    # dashboard Restore button, or two admins restoring the same world at
    # once, must not race two pushes of the same marker file and two
    # restart_container calls against the same container.
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    trigger_reconcile()
    backup_id = client.get(
        "/api/v1/worlds/world-overworld/backups", headers=auth_header(operator_token)
    ).json()[0]["id"]

    fake_lxd._restores_in_flight.add(("node-a", "world-overworld"))

    resp = client.post(
        f"/api/v1/worlds/world-overworld/backups/{backup_id}/restore",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409
    # Rejected before ever reaching push_file — no marker armed.
    marker_path = f"{NODE_WORLD_DIR}/.pending-restore.tar.gz"
    assert ("node-a", "world-overworld", marker_path) not in fake_lxd.pushed_files


def test_restore_backup_cleans_up_pending_marker_when_restart_fails(
    client, admin_token, operator_token, fake_lxd, trigger_reconcile
):
    # push_file succeeds (the marker is armed), but restart_container then
    # fails — without cleanup, the armed marker would sit on disk and get
    # silently applied on some later unrelated restart (see
    # CLAUDE.md's World backups entry / node/src/folia_node/agent.py's
    # _apply_pending_restore).
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    trigger_reconcile()
    backup_id = client.get(
        "/api/v1/worlds/world-overworld/backups", headers=auth_header(operator_token)
    ).json()[0]["id"]

    fake_lxd.fail_restart_with["world-overworld"] = LXDError("simulated restart failure")

    resp = client.post(
        f"/api/v1/worlds/world-overworld/backups/{backup_id}/restore",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 502

    marker_path = f"{NODE_WORLD_DIR}/.pending-restore.tar.gz"
    assert ("node-a", "world-overworld", marker_path) in fake_lxd.pushed_files
    assert ("node-a", "world-overworld", marker_path) in fake_lxd.deleted_files


def test_restore_backup_survives_marker_cleanup_itself_failing(
    client, admin_token, operator_token, fake_lxd, trigger_reconcile
):
    # If restart AND the best-effort marker cleanup both fail, the
    # endpoint must still fail cleanly (502) rather than raising an
    # unhandled exception.
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    trigger_reconcile()
    backup_id = client.get(
        "/api/v1/worlds/world-overworld/backups", headers=auth_header(operator_token)
    ).json()[0]["id"]

    fake_lxd.fail_restart_with["world-overworld"] = LXDError("simulated restart failure")
    fake_lxd.fail_delete_file_for.add(f"{NODE_WORLD_DIR}/.pending-restore.tar.gz")

    resp = client.post(
        f"/api/v1/worlds/world-overworld/backups/{backup_id}/restore",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 502  # must not raise despite cleanup itself failing


def test_restore_unknown_backup_id_404s(client, admin_token, operator_token):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.post(
        "/api/v1/worlds/world-overworld/backups/999/restore",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404
