from __future__ import annotations

from helpers import auth_header


def test_list_plugins_requires_auth(client):
    resp = client.get("/api/v1/plugins")
    assert resp.status_code == 401


def test_list_plugins_returns_catalog(client, viewer_token):
    resp = client.get("/api/v1/plugins", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()}
    assert "LuckPerms" in ids
    assert "Spark" in ids


def test_list_plugins_filters_by_category(client, viewer_token):
    resp = client.get("/api/v1/plugins", params={"category": "permissions"}, headers=auth_header(viewer_token))
    assert resp.status_code == 200
    body = resp.json()
    assert all(p["category"] == "permissions" for p in body)
    assert any(p["id"] == "LuckPerms" for p in body)


def test_get_plugin_by_id(client, viewer_token):
    resp = client.get("/api/v1/plugins/LuckPerms", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "LuckPerms"
    assert body["verified"] is True


def test_get_unknown_plugin_404s(client, viewer_token):
    resp = client.get("/api/v1/plugins/NotARealPlugin", headers=auth_header(viewer_token))
    assert resp.status_code == 404
