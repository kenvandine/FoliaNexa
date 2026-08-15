from __future__ import annotations

from helpers import auth_header


def test_login_rejects_bad_password(client, admin_token):
    resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_login_rejects_unknown_user(client):
    resp = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


def test_viewer_cannot_create_world(client, viewer_token):
    resp = client.post(
        "/api/v1/worlds",
        json={"name": "world-lobby", "type": "lobby", "cpu_cores": 1, "memory_gb": 1},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


def test_operator_cannot_manage_hosts_join_token(client, operator_token):
    resp = client.post("/api/v1/hosts/join-token", headers=auth_header(operator_token))
    assert resp.status_code == 403


def test_admin_can_manage_hosts_join_token(client, admin_token):
    resp = client.post("/api/v1/hosts/join-token", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert "token" in resp.json()


def test_missing_bearer_token_rejected(client):
    resp = client.get("/api/v1/worlds")
    assert resp.status_code == 401


def test_admin_can_create_users(client, admin_token):
    resp = client.post(
        "/api/v1/users",
        json={"username": "newop", "password": "hunter2", "role": "operator"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "operator"


def test_duplicate_username_rejected(client, admin_token):
    body = {"username": "dupe", "password": "hunter2", "role": "viewer"}
    first = client.post("/api/v1/users", json=body, headers=auth_header(admin_token))
    assert first.status_code == 200
    second = client.post("/api/v1/users", json=body, headers=auth_header(admin_token))
    assert second.status_code == 409
