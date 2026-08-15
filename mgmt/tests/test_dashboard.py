from __future__ import annotations


def test_dashboard_served_at_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "folia-nexa-mgmt" in resp.text


def test_api_routes_not_shadowed_by_static_mount(client):
    resp = client.get("/api/v1/worlds")
    assert resp.status_code == 401  # auth required, but it's the API handler, not a 404 from StaticFiles


def test_healthz_not_shadowed_by_static_mount(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_unknown_static_path_404s(client):
    resp = client.get("/no-such-file.html")
    assert resp.status_code == 404
