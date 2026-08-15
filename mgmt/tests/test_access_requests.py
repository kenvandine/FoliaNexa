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
