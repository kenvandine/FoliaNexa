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
