from __future__ import annotations

import json

from folia_mgmt.access_apply import NODE_WORLD_DIR, build_ops_json
from helpers import auth_header


def test_build_ops_json_resolves_uuids(fake_uuid_resolver):
    content = build_ops_json(["Steve", "Alex"], fake_uuid_resolver)
    entries = json.loads(content)

    names = {e["name"] for e in entries}
    assert names == {"Steve", "Alex"}
    for entry in entries:
        assert entry["level"] == 4
        assert entry["bypassesPlayerLimit"] is False
        assert entry["uuid"] == fake_uuid_resolver(entry["name"])


def test_build_ops_json_skips_unresolvable_names(fake_uuid_resolver):
    content = build_ops_json(["Steve", "unresolvable"], fake_uuid_resolver)
    entries = json.loads(content)
    assert [e["name"] for e in entries] == ["Steve"]


def test_build_ops_json_empty_list():
    content = build_ops_json([], lambda name: "irrelevant")
    assert json.loads(content) == []


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


def test_put_access_pushes_ops_json_for_placed_world(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.put(
        "/api/v1/worlds/world-overworld/access",
        json={"ops": ["Steve"]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200

    key = ("node-a", "world-overworld", f"{NODE_WORLD_DIR}/ops.json")
    assert key in fake_lxd.pushed_files
    entries = json.loads(fake_lxd.pushed_files[key])
    assert entries[0]["name"] == "Steve"


def test_put_access_does_not_push_when_world_not_placed(client, operator_token, fake_lxd):
    client.post(
        "/api/v1/worlds",
        json={"name": "world-stuck", "type": "nether", "cpu_cores": 2, "memory_gb": 3},
        headers=auth_header(operator_token),
    )
    resp = client.put(
        "/api/v1/worlds/world-stuck/access",
        json={"ops": ["Steve"]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200
    assert fake_lxd.pushed_files == {}


def test_put_access_skips_ops_push_when_ops_not_in_request_body(client, admin_token, operator_token, fake_lxd):
    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.put(
        "/api/v1/worlds/world-overworld/access",
        json={"whitelist_enabled": True},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200

    ops_key = ("node-a", "world-overworld", f"{NODE_WORLD_DIR}/ops.json")
    whitelist_key = ("node-a", "world-overworld", f"{NODE_WORLD_DIR}/whitelist.json")
    assert ops_key not in fake_lxd.pushed_files
    # whitelist_enabled *was* in the body, so its push does happen — see
    # test_put_access_pushes_whitelist_json_mirroring_approved_requests
    assert whitelist_key in fake_lxd.pushed_files


def test_put_access_pushes_whitelist_json_mirroring_approved_requests(
    client, admin_token, operator_token, fake_lxd, app
):
    from sqlmodel import Session

    from folia_mgmt.db import get_engine
    from folia_mgmt.models import AccessRequest, AccessRequestStatus

    engine = get_engine(app.state.test_settings)
    with Session(engine) as session:
        session.add(
            AccessRequest(
                discord_user_id="1",
                discord_username="somebody",
                minecraft_username="Steve",
                minecraft_uuid="069a79f444e94726a5befca90e38aaf9",
                status=AccessRequestStatus.approved,
            )
        )
        session.add(
            AccessRequest(
                discord_user_id="2",
                discord_username="pending-person",
                minecraft_username="NotApprovedYet",
                minecraft_uuid="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                status=AccessRequestStatus.pending,
            )
        )
        session.commit()

    _enroll_host(client, admin_token)
    client.post(
        "/api/v1/worlds",
        json={"name": "world-overworld", "type": "overworld", "cpu_cores": 4, "memory_gb": 8},
        headers=auth_header(operator_token),
    )

    resp = client.put(
        "/api/v1/worlds/world-overworld/access",
        json={"whitelist_enabled": True},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200

    key = ("node-a", "world-overworld", f"{NODE_WORLD_DIR}/whitelist.json")
    entries = json.loads(fake_lxd.pushed_files[key])
    assert entries == [{"uuid": "069a79f444e94726a5befca90e38aaf9", "name": "Steve"}]
