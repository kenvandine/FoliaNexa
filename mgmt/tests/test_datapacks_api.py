from __future__ import annotations

from helpers import auth_header


def test_list_datapacks_requires_auth(client):
    resp = client.get("/api/v1/datapacks")
    assert resp.status_code == 401


def test_list_datapacks_returns_catalog(client, viewer_token):
    resp = client.get("/api/v1/datapacks", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()}
    assert "Matcha" in ids


def test_list_datapacks_filters_by_category(client, viewer_token):
    resp = client.get("/api/v1/datapacks", params={"category": "gameplay-tweaks"}, headers=auth_header(viewer_token))
    assert resp.status_code == 200
    body = resp.json()
    assert all(p["category"] == "gameplay-tweaks" for p in body)
    assert any(p["id"] == "Matcha" for p in body)


def test_get_datapack_by_id(client, viewer_token):
    resp = client.get("/api/v1/datapacks/Matcha", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "Matcha"
    assert body["verified"] is True


def test_get_unknown_datapack_404s(client, viewer_token):
    resp = client.get("/api/v1/datapacks/NotARealDatapack", headers=auth_header(viewer_token))
    assert resp.status_code == 404
