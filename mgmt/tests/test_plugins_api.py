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


def test_put_plugin_expect_new_allows_a_genuinely_new_id(client, operator_token):
    resp = client.put(
        "/api/v1/plugins/MyNewPlugin",
        params={"expect_new": "true"},
        json={"category": "misc", "source": "in-house", "version": "1.0.0"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text


def test_put_plugin_expect_new_rejects_colliding_bundled_id(client, operator_token):
    resp = client.put(
        "/api/v1/plugins/LuckPerms",
        params={"expect_new": "true"},
        json={"category": "permissions", "source": "external", "version": "99.9.9"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 409
    # and the bundled entry itself is untouched
    get_resp = client.get("/api/v1/plugins/LuckPerms", headers=auth_header(operator_token))
    assert get_resp.json()["is_override"] is False


def test_put_plugin_expect_new_rejects_colliding_override_id(client, operator_token):
    client.put(
        "/api/v1/plugins/MyNewPlugin",
        json={"category": "misc", "source": "in-house", "version": "1.0.0"},
        headers=auth_header(operator_token),
    )
    resp = client.put(
        "/api/v1/plugins/MyNewPlugin",
        params={"expect_new": "true"},
        json={"category": "misc", "source": "in-house", "version": "2.0.0"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 409
    get_resp = client.get("/api/v1/plugins/MyNewPlugin", headers=auth_header(operator_token))
    assert get_resp.json()["version"] == "1.0.0"  # untouched


def test_put_plugin_without_expect_new_still_overwrites_freely(client, operator_token):
    """The default (dashboard's "Edit" form) — overwriting on purpose is
    the whole point there, same behavior as before expect_new existed."""
    resp = client.put(
        "/api/v1/plugins/LuckPerms",
        json={"category": "permissions", "source": "external", "version": "99.9.9"},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text


def _upload_jar(client, operator_token, plugin_yml: str) -> dict:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.yml", plugin_yml)
    resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("plugin.jar", buf.getvalue(), "application/java-archive")},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_put_plugin_replacing_an_uploaded_jar_deletes_the_old_one(client, operator_token, app):
    from folia_mgmt.plugin_upload import uploaded_jar_dir_from_url

    settings = app.state.test_settings
    first = _upload_jar(client, operator_token, "name: MyCommercialPlugin\nversion: '1.0.0'\n")
    client.put(
        "/api/v1/plugins/MyCommercialPlugin",
        json={"category": "misc", "source": "external", "version": "1.0.0", "download_url": first["download_url"]},
        headers=auth_header(operator_token),
    )
    old_dir = uploaded_jar_dir_from_url(settings, first["download_url"])
    assert old_dir is not None and old_dir.is_dir()

    second = _upload_jar(client, operator_token, "name: MyCommercialPlugin\nversion: '2.0.0'\n")
    resp = client.put(
        "/api/v1/plugins/MyCommercialPlugin",
        json={"category": "misc", "source": "external", "version": "2.0.0", "download_url": second["download_url"]},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text

    assert not old_dir.exists()  # superseded upload cleaned up
    new_dir = uploaded_jar_dir_from_url(settings, second["download_url"])
    assert new_dir is not None and new_dir.is_dir()  # current upload untouched


def test_put_plugin_with_unchanged_download_url_does_not_delete_it(client, operator_token, app):
    from folia_mgmt.plugin_upload import uploaded_jar_dir_from_url

    settings = app.state.test_settings
    uploaded = _upload_jar(client, operator_token, "name: MyCommercialPlugin\nversion: '1.0.0'\n")
    body = {"category": "misc", "source": "external", "version": "1.0.0", "download_url": uploaded["download_url"]}
    client.put("/api/v1/plugins/MyCommercialPlugin", json=body, headers=auth_header(operator_token))
    # re-save with the same download_url (e.g. just flipping `verified`)
    resp = client.put(
        "/api/v1/plugins/MyCommercialPlugin",
        json={**body, "verified": True},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text

    directory = uploaded_jar_dir_from_url(settings, uploaded["download_url"])
    assert directory is not None and directory.is_dir()


def test_delete_plugin_removes_its_uploaded_jar(client, operator_token, app):
    from folia_mgmt.plugin_upload import uploaded_jar_dir_from_url

    settings = app.state.test_settings
    uploaded = _upload_jar(client, operator_token, "name: MyCommercialPlugin\nversion: '1.0.0'\n")
    client.put(
        "/api/v1/plugins/MyCommercialPlugin",
        json={"category": "misc", "source": "external", "version": "1.0.0", "download_url": uploaded["download_url"]},
        headers=auth_header(operator_token),
    )
    directory = uploaded_jar_dir_from_url(settings, uploaded["download_url"])
    assert directory is not None and directory.is_dir()

    resp = client.delete("/api/v1/plugins/MyCommercialPlugin", headers=auth_header(operator_token))
    assert resp.status_code == 200, resp.text

    assert not directory.exists()
