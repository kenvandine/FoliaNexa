from __future__ import annotations

from sqlmodel import Session

from folia_mgmt.db import get_engine
from folia_mgmt.models import AccessRequest, AccessRequestStatus
from helpers import auth_header


def _insert_request(app, **overrides) -> int:
    engine = get_engine(app.state.test_settings)
    defaults = dict(
        discord_user_id="123456789",
        discord_username="somebody",
        minecraft_username="Steve",
        minecraft_uuid="069a79f444e94726a5befca90e38aaf9",
        status=AccessRequestStatus.pending,
    )
    defaults.update(overrides)
    with Session(engine) as session:
        request = AccessRequest(**defaults)
        session.add(request)
        session.commit()
        session.refresh(request)
        return request.id


def test_list_requires_operator(client, viewer_token, app):
    _insert_request(app)
    resp = client.get("/api/v1/access-requests", headers=auth_header(viewer_token))
    assert resp.status_code == 403


def test_list_pending_requests(client, operator_token, app):
    _insert_request(app, discord_username="alice")
    _insert_request(app, discord_username="bob", status=AccessRequestStatus.approved, discord_user_id="999")

    resp = client.get("/api/v1/access-requests?status_filter=pending", headers=auth_header(operator_token))
    assert resp.status_code == 200
    names = {r["discord_username"] for r in resp.json()}
    assert names == {"alice"}


def test_approve_request(client, operator_token, app):
    request_id = _insert_request(app)
    resp = client.post(f"/api/v1/access-requests/{request_id}/approve", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


def test_deny_request_with_reason(client, operator_token, app):
    request_id = _insert_request(app)
    resp = client.post(
        f"/api/v1/access-requests/{request_id}/deny",
        json={"reason": "not in the Discord server"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"


def test_decide_unknown_request_404s(client, operator_token):
    resp = client.post("/api/v1/access-requests/9999/approve", headers=auth_header(operator_token))
    assert resp.status_code == 404


def test_approved_uuids_only_includes_approved_with_uuid(client, viewer_token, app):
    _insert_request(app, discord_user_id="1", status=AccessRequestStatus.approved, minecraft_uuid="069a79f444e94726a5befca90e38aaf9")
    _insert_request(app, discord_user_id="2", status=AccessRequestStatus.pending, minecraft_uuid="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    _insert_request(app, discord_user_id="3", status=AccessRequestStatus.approved, minecraft_uuid=None)

    resp = client.get("/api/v1/access-requests/approved-uuids", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    assert resp.json()["uuids"] == ["069a79f444e94726a5befca90e38aaf9"]


def test_approved_uuids_accessible_by_viewer_role(client, viewer_token):
    resp = client.get("/api/v1/access-requests/approved-uuids", headers=auth_header(viewer_token))
    assert resp.status_code == 200


def _fake_resolver(monkeypatch, mapping: dict[str, str | None]) -> None:
    monkeypatch.setattr(
        "folia_mgmt.routers.access_requests.resolve_minecraft_uuid",
        lambda name: mapping.get(name),
    )


def test_create_access_request_requires_operator(client, viewer_token):
    resp = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve"},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


def test_create_access_request_lands_pending_by_default(client, operator_token, monkeypatch):
    _fake_resolver(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38aaf9"})
    resp = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["minecraft_uuid"] == "069a79f444e94726a5befca90e38aaf9"
    assert body["auto_approved"] is False


def test_create_access_request_auto_approve_true(client, operator_token, monkeypatch):
    _fake_resolver(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38aaf9"})
    resp = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve", "auto_approve": True},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["auto_approved"] is True

    # and it shows up where the proxy's access gate looks
    uuids = client.get("/api/v1/access-requests/approved-uuids", headers=auth_header(operator_token)).json()
    assert uuids["uuids"] == ["069a79f444e94726a5befca90e38aaf9"]


def test_create_access_request_upserts_by_discord_user_id(client, operator_token, monkeypatch):
    _fake_resolver(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38aaf9", "SteveAlt": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
    first = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve"},
        headers=auth_header(operator_token),
    )
    second = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "SteveAlt"},
        headers=auth_header(operator_token),
    )
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["minecraft_username"] == "SteveAlt"

    all_requests = client.get("/api/v1/access-requests", headers=auth_header(operator_token)).json()
    assert len(all_requests) == 1


def test_create_access_request_uses_supplied_bedrock_uuid_without_mojang_lookup(client, operator_token, monkeypatch):
    called = []

    def tracking_resolver(name):
        called.append(name)
        return "should-not-be-used"

    monkeypatch.setattr("folia_mgmt.routers.access_requests.resolve_minecraft_uuid", tracking_resolver)

    resp = client.post(
        "/api/v1/access-requests",
        json={
            "discord_user_id": "1",
            "discord_username": "bob",
            "minecraft_username": "SomeBedrockGamertag",
            "minecraft_uuid": "00000000-0000-0000-0009-0009000000f4",
        },
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["minecraft_uuid"] == "00000000-0000-0000-0009-0009000000f4"
    assert called == []


def test_create_access_request_does_not_reapprove_already_denied(client, operator_token, monkeypatch):
    _fake_resolver(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38aaf9"})
    create = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve"},
        headers=auth_header(operator_token),
    )
    request_id = create.json()["id"]
    client.post(f"/api/v1/access-requests/{request_id}/deny", json={}, headers=auth_header(operator_token))

    resp = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve", "auto_approve": True},
        headers=auth_header(operator_token),
    )
    # auto_approve only fires from `pending` — a denied request needs an
    # explicit human re-approval, not another bot round trip to flip it
    assert resp.json()["status"] == "denied"
    assert resp.json()["auto_approved"] is False


def test_create_access_request_defaults_auto_managed_true(client, operator_token, monkeypatch):
    _fake_resolver(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38aaf9"})
    resp = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve"},
        headers=auth_header(operator_token),
    )
    assert resp.json()["auto_managed"] is True


def test_create_access_request_honors_auto_managed_false(client, operator_token, monkeypatch):
    _fake_resolver(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38aaf9"})
    resp = client.post(
        "/api/v1/access-requests",
        json={
            "discord_user_id": "manual:Steve",
            "discord_username": "Steve",
            "minecraft_username": "Steve",
            "auto_approve": True,
            "auto_managed": False,
        },
        headers=auth_header(operator_token),
    )
    assert resp.json()["auto_managed"] is False


def test_approve_and_deny_endpoints_mark_row_not_auto_managed(client, operator_token, app):
    approve_id = _insert_request(app, discord_user_id="1")
    deny_id = _insert_request(app, discord_user_id="2")

    approved = client.post(f"/api/v1/access-requests/{approve_id}/approve", headers=auth_header(operator_token))
    denied = client.post(f"/api/v1/access-requests/{deny_id}/deny", json={}, headers=auth_header(operator_token))

    assert approved.json()["auto_managed"] is False
    assert denied.json()["auto_managed"] is False


def test_repeat_create_access_request_does_not_undo_a_sticky_human_decision(client, operator_token, monkeypatch):
    """A player denied by a human, then running /request-access again,
    must not silently become role-sync-eligible again — that requires an
    explicit human re-decision, matching the existing "does not reapprove
    already denied" invariant above, now extended to auto_managed."""
    _fake_resolver(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38aaf9"})
    create = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve"},
        headers=auth_header(operator_token),
    )
    request_id = create.json()["id"]
    client.post(f"/api/v1/access-requests/{request_id}/deny", json={}, headers=auth_header(operator_token))

    resp = client.post(
        "/api/v1/access-requests",
        json={"discord_user_id": "1", "discord_username": "bob", "minecraft_username": "Steve"},
        headers=auth_header(operator_token),
    )
    assert resp.json()["auto_managed"] is False


def test_get_gate_config_defaults(client, viewer_token):
    resp = client.get("/api/v1/access-requests/discord-gate-config", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "guild_id": None, "role_id": None}


def test_update_gate_config_requires_operator(client, viewer_token):
    resp = client.put(
        "/api/v1/access-requests/discord-gate-config",
        json={"enabled": True, "guild_id": "1537925612952363008", "role_id": "1537937124144193637"},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


def test_update_gate_config_round_trips(client, operator_token, viewer_token):
    put_resp = client.put(
        "/api/v1/access-requests/discord-gate-config",
        json={"enabled": True, "guild_id": "1537925612952363008", "role_id": "1537937124144193637"},
        headers=auth_header(operator_token),
    )
    assert put_resp.status_code == 200
    assert put_resp.json() == {
        "enabled": True,
        "guild_id": "1537925612952363008",
        "role_id": "1537937124144193637",
    }

    get_resp = client.get("/api/v1/access-requests/discord-gate-config", headers=auth_header(viewer_token))
    assert get_resp.json() == put_resp.json()


def _enable_gate(client, operator_token) -> None:
    client.put(
        "/api/v1/access-requests/discord-gate-config",
        json={"enabled": True, "guild_id": "1537925612952363008", "role_id": "1537937124144193637"},
        headers=auth_header(operator_token),
    )


def test_role_sync_requires_operator(client, viewer_token):
    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": []},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


def test_role_sync_noops_when_gate_disabled(client, operator_token, app):
    request_id = _insert_request(app, discord_user_id="1", status=AccessRequestStatus.pending)
    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": ["1"]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"approved": [], "revoked": []}

    listed = client.get("/api/v1/access-requests", headers=auth_header(operator_token)).json()
    assert [r for r in listed if r["id"] == request_id][0]["status"] == "pending"


def test_role_sync_approves_managed_pending_row_with_role(client, operator_token, app):
    _enable_gate(client, operator_token)
    request_id = _insert_request(app, discord_user_id="1", status=AccessRequestStatus.pending)

    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": ["1"]},
        headers=auth_header(operator_token),
    )
    assert resp.json() == {"approved": ["1"], "revoked": []}

    listed = client.get("/api/v1/access-requests", headers=auth_header(operator_token)).json()
    assert [r for r in listed if r["id"] == request_id][0]["status"] == "approved"


def test_role_sync_revokes_managed_approved_row_missing_role(client, operator_token, app):
    _enable_gate(client, operator_token)
    request_id = _insert_request(app, discord_user_id="1", status=AccessRequestStatus.approved)

    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": ["someone-else"]},
        headers=auth_header(operator_token),
    )
    assert resp.json() == {"approved": [], "revoked": ["1"]}

    listed = client.get("/api/v1/access-requests", headers=auth_header(operator_token)).json()
    row = [r for r in listed if r["id"] == request_id][0]
    assert row["status"] == "revoked"


def test_role_sync_reapproves_previously_revoked_row_when_role_regained(client, operator_token, app):
    _enable_gate(client, operator_token)
    request_id = _insert_request(app, discord_user_id="1", status=AccessRequestStatus.approved)

    client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": []},
        headers=auth_header(operator_token),
    )
    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": ["1"]},
        headers=auth_header(operator_token),
    )
    assert resp.json() == {"approved": ["1"], "revoked": []}

    listed = client.get("/api/v1/access-requests", headers=auth_header(operator_token)).json()
    assert [r for r in listed if r["id"] == request_id][0]["status"] == "approved"


def test_role_sync_never_touches_human_decided_row(client, operator_token, app):
    _enable_gate(client, operator_token)
    request_id = _insert_request(app, discord_user_id="1", status=AccessRequestStatus.denied, auto_managed=False)

    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": ["1"]},
        headers=auth_header(operator_token),
    )
    assert resp.json() == {"approved": [], "revoked": []}

    listed = client.get("/api/v1/access-requests", headers=auth_header(operator_token)).json()
    assert [r for r in listed if r["id"] == request_id][0]["status"] == "denied"


def test_role_sync_skips_unknown_discord_ids(client, operator_token):
    _enable_gate(client, operator_token)
    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": ["999999"]},
        headers=auth_header(operator_token),
    )
    assert resp.json() == {"approved": [], "revoked": []}

    listed = client.get("/api/v1/access-requests", headers=auth_header(operator_token)).json()
    assert listed == []


def test_role_sync_manual_allowlist_entry_survives_missing_role(client, operator_token, monkeypatch):
    """The cross-feature regression this pairing exists to prevent: a
    manually-added allowlist entry (auto_managed=False, synthetic
    discord_user_id) must never be revoked by role-sync just because its
    fake id can never appear in a real Discord role-holder set."""
    _enable_gate(client, operator_token)
    _fake_resolver(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38aaf9"})
    create = client.post(
        "/api/v1/access-requests",
        json={
            "discord_user_id": "manual:Steve",
            "discord_username": "Steve",
            "minecraft_username": "Steve",
            "auto_approve": True,
            "auto_managed": False,
        },
        headers=auth_header(operator_token),
    )
    assert create.json()["status"] == "approved"

    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": ["someone-else"]},
        headers=auth_header(operator_token),
    )
    assert resp.json() == {"approved": [], "revoked": []}

    uuids = client.get("/api/v1/access-requests/approved-uuids", headers=auth_header(operator_token)).json()
    assert uuids["uuids"] == ["069a79f444e94726a5befca90e38aaf9"]


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


def test_role_sync_triggers_immediate_whitelist_push(client, admin_token, operator_token, app, fake_lxd):
    _enable_gate(client, operator_token)
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )
    client.put(
        "/api/v1/worlds/world-overworld/access",
        json={"whitelist_enabled": True},
        headers=auth_header(operator_token),
    )
    fake_lxd.pushed_files.clear()  # drop the push from the toggle itself, isolate role-sync's own push

    _insert_request(app, discord_user_id="1", status=AccessRequestStatus.pending)
    resp = client.post(
        "/api/v1/access-requests/role-sync",
        json={"discord_user_ids_with_role": ["1"]},
        headers=auth_header(operator_token),
    )
    assert resp.json() == {"approved": ["1"], "revoked": []}
    assert ("node-a", "world-overworld", "/var/snap/folia-nexa-node/common/world/whitelist.json") in fake_lxd.pushed_files
