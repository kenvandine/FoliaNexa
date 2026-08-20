from __future__ import annotations

import io
import zipfile

import pytest
from helpers import auth_header

from folia_mgmt.config import Settings
from folia_mgmt.plugin_upload import InvalidJarError, inspect_jar, save_uploaded_jar, uploaded_jar_download_url


def _jar_bytes(plugin_yml: str | None, filename: str = "plugin.yml") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if plugin_yml is not None:
            zf.writestr(filename, plugin_yml)
        zf.writestr("com/example/Main.class", b"\x00\x01")
    return buf.getvalue()


VALID_PLUGIN_YML = """
name: MyCommercialPlugin
version: "2.3.1"
main: com.example.Main
website: https://example.com/myplugin
folia-supported: true
"""


def test_inspect_jar_no_yaml_returns_all_none():
    metadata = inspect_jar(_jar_bytes(None))
    assert metadata.name is None
    assert metadata.version is None
    assert metadata.folia_supported is None


def test_inspect_jar_bad_zip_raises():
    with pytest.raises(InvalidJarError):
        inspect_jar(b"definitely not a zip")


def test_inspect_jar_prefers_paper_plugin_yml():
    jar = _jar_bytes(VALID_PLUGIN_YML, filename="paper-plugin.yml")
    metadata = inspect_jar(jar)
    assert metadata.name == "MyCommercialPlugin"


def test_uploaded_jar_download_url_requires_public_url(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", public_url=None)
    token, filename = save_uploaded_jar(settings, "plugin.jar", b"content")
    with pytest.raises(ValueError):
        uploaded_jar_download_url(settings, token, filename)


def test_save_uploaded_jar_sanitizes_path_traversal(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    token, filename = save_uploaded_jar(settings, "../../etc/passwd.jar", b"x")
    assert "/" not in filename and ".." not in filename
    stored = settings.plugin_uploads_dir / token / filename
    assert stored.exists()
    assert stored.is_relative_to(settings.plugin_uploads_dir)


def test_upload_requires_operator(client, viewer_token):
    resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("plugin.jar", _jar_bytes(VALID_PLUGIN_YML), "application/java-archive")},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


def test_upload_requires_auth(client):
    resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("plugin.jar", _jar_bytes(VALID_PLUGIN_YML), "application/java-archive")},
    )
    assert resp.status_code == 401


def test_upload_extracts_metadata_from_plugin_yml(client, operator_token):
    resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("plugin.jar", _jar_bytes(VALID_PLUGIN_YML), "application/java-archive")},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggested_id"] == "MyCommercialPlugin"
    assert body["suggested_version"] == "2.3.1"
    assert body["folia_supported"] is True
    assert body["website"] == "https://example.com/myplugin"
    assert body["download_url"].startswith("https://mgmt.example:8443/plugin-jars/")
    assert body["download_url"].endswith(".jar")
    assert len(body["sha256"]) == 64


def test_upload_flags_folia_unsupported(client, operator_token):
    yml = VALID_PLUGIN_YML.replace("folia-supported: true", "folia-supported: false")
    resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("plugin.jar", _jar_bytes(yml), "application/java-archive")},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["folia_supported"] is False


def test_upload_with_no_plugin_yml_still_succeeds(client, operator_token):
    resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("mystery.jar", _jar_bytes(None), "application/java-archive")},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggested_id"] is None
    assert body["suggested_version"] is None
    assert body["folia_supported"] is None
    assert body["download_url"]


def test_uploaded_jar_bytes_match_original(client, operator_token):
    original = _jar_bytes(VALID_PLUGIN_YML)
    resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("plugin.jar", original, "application/java-archive")},
        headers=auth_header(operator_token),
    )
    body = resp.json()
    download_path = body["download_url"].removeprefix("https://mgmt.example:8443")
    fetched = client.get(download_path)
    assert fetched.status_code == 200
    assert fetched.content == original


def test_upload_rejects_non_zip_content(client, operator_token):
    resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("plugin.jar", b"not a jar at all", "application/java-archive")},
        headers=auth_header(operator_token),
    )
    assert resp.status_code == 400


def test_upload_two_files_get_different_private_urls(client, operator_token):
    def upload():
        resp = client.post(
            "/api/v1/plugins/upload",
            files={"file": ("plugin.jar", _jar_bytes(VALID_PLUGIN_YML), "application/java-archive")},
            headers=auth_header(operator_token),
        )
        return resp.json()["download_url"]

    assert upload() != upload()


def test_upload_result_can_be_saved_as_catalog_entry(client, operator_token):
    upload_resp = client.post(
        "/api/v1/plugins/upload",
        files={"file": ("plugin.jar", _jar_bytes(VALID_PLUGIN_YML), "application/java-archive")},
        headers=auth_header(operator_token),
    )
    body = upload_resp.json()

    put_resp = client.put(
        f"/api/v1/plugins/{body['suggested_id']}",
        json={
            "category": "commercial",
            "source": "external",
            "version": body["suggested_version"],
            "download_url": body["download_url"],
            "sha256": body["sha256"],
            "verified": True,
        },
        headers=auth_header(operator_token),
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["download_url"] == body["download_url"]
