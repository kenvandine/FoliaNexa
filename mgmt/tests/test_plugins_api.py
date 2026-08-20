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
    assert "Chunky" in ids


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


def test_list_plugins_marks_which_ids_are_overridden(client, viewer_token, operator_token):
    resp = client.get("/api/v1/plugins", headers=auth_header(viewer_token))
    assert all(p["is_override"] is False for p in resp.json())

    client.put(
        "/api/v1/plugins/LuckPerms",
        json={"category": "permissions", "source": "external", "version": "99.9.9"},
        headers=auth_header(operator_token),
    )
    resp = client.get("/api/v1/plugins", headers=auth_header(viewer_token))
    by_id = {p["id"]: p for p in resp.json()}
    assert by_id["LuckPerms"]["is_override"] is True
    assert by_id["Chunky"]["is_override"] is False


def test_put_plugin_adds_a_new_entry(client, operator_token, viewer_token):
    resp = client.put(
        "/api/v1/plugins/MyNewPlugin",
        json={
            "category": "misc",
            "source": "in-house",
            "version": "1.0.0",
            "download_url": "https://example.internal/my-new-plugin.jar",
            "sha256": "a" * 64,
            "verified": True,
        },
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_override"] is True

    get_resp = client.get("/api/v1/plugins/MyNewPlugin", headers=auth_header(viewer_token))
    assert get_resp.status_code == 200
    assert get_resp.json()["download_url"] == "https://example.internal/my-new-plugin.jar"


def test_put_plugin_edits_an_existing_bundled_entry(client, operator_token, viewer_token):
    resp = client.put(
        "/api/v1/plugins/LuckPerms",
        json={
            "category": "permissions",
            "source": "external",
            "version": "99.9.9",
            "download_url": "https://mirror.internal/luckperms-pinned.jar",
            "verified": True,
        },
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "99.9.9"
    assert body["download_url"] == "https://mirror.internal/luckperms-pinned.jar"


def test_put_plugin_rejects_invalid_source(client, operator_token):
    resp = client.put(
        "/api/v1/plugins/Whatever",
        json={"category": "misc", "source": "not-a-real-source", "version": "1.0.0"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 422


def test_put_plugin_requires_operator_role(client, viewer_token):
    resp = client.put(
        "/api/v1/plugins/Whatever",
        json={"category": "misc", "source": "external", "version": "1.0.0"},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


def test_delete_plugin_override_reverts_to_bundled(client, operator_token, viewer_token):
    client.put(
        "/api/v1/plugins/LuckPerms",
        json={"category": "permissions", "source": "external", "version": "99.9.9"},
        headers=auth_header(operator_token),
    )
    resp = client.delete("/api/v1/plugins/LuckPerms", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"removed_override": True, "reverted_to_bundled": True}

    get_resp = client.get("/api/v1/plugins/LuckPerms", headers=auth_header(viewer_token))
    assert get_resp.json()["version"] != "99.9.9"
    assert get_resp.json()["is_override"] is False


def test_delete_plugin_override_only_entry_removes_entirely(client, operator_token, viewer_token):
    client.put(
        "/api/v1/plugins/MyNewPlugin",
        json={"category": "misc", "source": "in-house", "version": "1.0.0"},
        headers=auth_header(operator_token),
    )
    resp = client.delete("/api/v1/plugins/MyNewPlugin", headers=auth_header(operator_token))
    assert resp.json() == {"removed_override": True, "reverted_to_bundled": False}

    get_resp = client.get("/api/v1/plugins/MyNewPlugin", headers=auth_header(viewer_token))
    assert get_resp.status_code == 404


def test_delete_nonexistent_override_404s(client, operator_token):
    resp = client.delete("/api/v1/plugins/NotOverridden", headers=auth_header(operator_token))
    assert resp.status_code == 404


def test_delete_plugin_requires_operator_role(client, viewer_token):
    resp = client.delete("/api/v1/plugins/LuckPerms", headers=auth_header(viewer_token))
    assert resp.status_code == 403
